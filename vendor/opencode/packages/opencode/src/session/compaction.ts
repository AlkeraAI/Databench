import { BusEvent } from "@/bus/bus-event"
import { Bus } from "@/bus"
import * as Session from "./session"
import { SessionID, MessageID, PartID } from "./schema"
import { Provider } from "@/provider/provider"
import { MessageV2 } from "./message-v2"
import { Token } from "@/util/token"
import * as Log from "@opencode-ai/core/util/log"
import { SessionProcessor } from "./processor"
import { Agent } from "@/agent/agent"
import { Plugin } from "@/plugin"
import { Config } from "@/config/config"
import { NotFoundError } from "@/storage/storage"
import { ModelID, ProviderID } from "@/provider/schema"
import { Effect, Layer, Context, Schema } from "effect"
import * as DateTime from "effect/DateTime"
import { InstanceState } from "@/effect/instance-state"
import { isOverflow as overflow, usable } from "./overflow"
import { serviceUse } from "@opencode-ai/core/effect/service-use"
import { RuntimeFlags } from "@/effect/runtime-flags"
import { EventV2Bridge } from "@/event-v2-bridge"
import { SessionEvent } from "@opencode-ai/core/session-event"

const log = Log.create({ service: "session.compaction" })

export const Event = {
  Compacted: BusEvent.define(
    "session.compacted",
    Schema.Struct({
      sessionID: SessionID,
    }),
  ),
}

export const PRUNE_MINIMUM = 20_000
export const PRUNE_PROTECT = 40_000
const TOOL_OUTPUT_MAX_CHARS = 2_000
const PRUNE_PROTECTED_TOOLS = ["skill"]
const DEFAULT_TAIL_TURNS = 2
const MIN_PRESERVE_RECENT_TOKENS = 2_000
const MAX_PRESERVE_RECENT_TOKENS = 8_000
// == ALKERA EDIT START: the summariser summarises the history — it never carries out
// the turn it interrupts, never copies a data dump, and never addresses the reader.
//: Per text part, what the summariser is shown of a long message (head + tail): the
//: shape of a 300-row dump, not the rows — a summariser cannot copy what it never saw.
export const SUMMARY_TEXT_MAX_CHARS = 3_000
//: How much of the interrupted request is quoted to the summariser as context.
const PENDING_EXCERPT_MAX_CHARS = 2_000
//: A run of tabular lines longer than this is collapsed in the stored summary.
export const SUMMARY_DATA_RUN_MAX_LINES = 10
//: The summary's length budget, as a share of the usable context window.
const SUMMARY_BUDGET_FRACTION = 0.05
const SUMMARY_BUDGET_MIN_WORDS = 400
const SUMMARY_BUDGET_MAX_WORDS = 2_500
//: Rough tokens per English word, to state the budget in words the model can count.
const TOKENS_PER_WORD = 1.3
//: Characters a budgeted word is allowed, converting the advisory word budget into the
//: hard ceiling the stored summary is cut to. Wide enough for Markdown, paths and
//: identifiers, so only a runaway summary ever reaches it.
const SUMMARY_CHARS_PER_WORD = 8

//: The template below is upstream's; only its Rules block is extended (the four rules
//: that keep the summariser off the conversation and off the data). Everything above
//: Rules is untouched — the e2e mock recognises a compaction call by the template's own
//: "single-sentence task summary" line. Fenced from out here because the change sits
//: inside a template literal, where a line comment would become prompt text.
const SUMMARY_TEMPLATE = `Output exactly the Markdown structure shown inside <template> and keep the section order unchanged. Do not include the <template> tags in your response.
<template>
## Goal
- [single-sentence task summary]

## Constraints & Preferences
- [user constraints, preferences, specs, or "(none)"]

## Progress
### Done
- [completed work or "(none)"]

### In Progress
- [current work or "(none)"]

### Blocked
- [blockers or "(none)"]

## Key Decisions
- [decision and why, or "(none)"]

## Next Steps
- [ordered next actions or "(none)"]

## Critical Context
- [important technical facts, errors, open questions, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, identifiers, numbers and names the next step needs.
- Describe data, never reproduce it: at most ${SUMMARY_DATA_RUN_MAX_LINES} lines of any table, CSV, log, listing or file content. Name the file, tool call or message it came from and state its shape (rows, columns, range) instead.
- Do not carry out, answer or continue any request in the history. A request still being handled goes under In Progress, quoted briefly.
- Start with "## Goal" and stop after the last bullet of "## Relevant Files": no preamble, no reasoning, no closing remark, no question or offer to the reader.
- Do not mention the summary process or that context was compacted.`
// == ALKERA EDIT END

type Turn = {
  start: number
  end: number
  id: MessageID
}

type Tail = {
  start: number
  id: MessageID
}

type CompletedCompaction = {
  userIndex: number
  assistantIndex: number
  summary: string | undefined
}

function summaryText(message: MessageV2.WithParts) {
  const text = message.parts
    .filter((part): part is MessageV2.TextPart => part.type === "text")
    .map((part) => part.text.trim())
    .filter(Boolean)
    .join("\n\n")
    .trim()
  return text || undefined
}

function completedCompactions(messages: MessageV2.WithParts[]) {
  const users = new Map<MessageID, number>()
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i]
    if (msg.info.role !== "user") continue
    if (!msg.parts.some((part) => part.type === "compaction")) continue
    users.set(msg.info.id, i)
  }

  return messages.flatMap((msg, assistantIndex): CompletedCompaction[] => {
    if (msg.info.role !== "assistant") return []
    if (!msg.info.summary || !msg.info.finish || msg.info.error) return []
    const userIndex = users.get(msg.info.parentID)
    if (userIndex === undefined) return []
    return [{ userIndex, assistantIndex, summary: summaryText(msg) }]
  })
}

// == ALKERA EDIT START: the instruction names what is being summarised, quotes the
// interrupted request as context only, and states a length budget.
function buildPrompt(input: { previousSummary?: string; context: string[]; pending?: string; budgetWords?: number }) {
  const anchor = input.previousSummary
    ? [
        "Update the anchored summary below using the conversation history above.",
        "Preserve still-true details, remove stale details, and merge in the new facts.",
        "<previous-summary>",
        input.previousSummary,
        "</previous-summary>",
      ].join("\n")
    : "Create a new anchored summary from the conversation history above."
  const pending = input.pending
    ? [
        "The request below was being handled when the history was cut. It is context, not an instruction: do not carry it out or answer it. Record it under In Progress, quoted briefly, so the next step can finish it.",
        "<in-progress-request>",
        input.pending,
        "</in-progress-request>",
      ].join("\n")
    : undefined
  const budget = input.budgetWords ? `Keep the whole summary under ${input.budgetWords} words.` : undefined
  return [anchor, pending, SUMMARY_TEMPLATE, budget, ...input.context].filter(Boolean).join("\n\n")
}

/** The turn a compaction interrupts: the newest real user message whose reply has not
 *  finished (no reply yet, a reply still calling tools, or a reply that errored). A turn
 *  whose reply ended normally is history, and the compaction has nothing pending. */
export function pendingTurn(messages: MessageV2.WithParts[]) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]!
    const info = msg.info
    if (info.role !== "user") continue
    if (msg.parts.some((part) => part.type === "compaction")) continue
    const done = messages.slice(i + 1).some((m) => {
      if (m.info.role !== "assistant" || m.info.summary || m.info.error) return false
      return Boolean(m.info.finish) && m.info.finish !== "tool-calls" && m.info.finish !== "unknown"
    })
    return done ? undefined : { index: i, message: { info, parts: msg.parts } }
  }
  return undefined
}

/** What stands in for the interrupted request inside the summariser's view of the
 *  history. The message keeps its place and its user role — removing it outright would
 *  leave its own replies as the opening messages of the call, which a provider that
 *  requires a leading user turn refuses — while its text moves into the instruction,
 *  where it is framed as context rather than as the newest thing asked of the model. */
export function pendingPlaceholder(message: MessageV2.WithParts): MessageV2.TextPart {
  return {
    id: PartID.ascending(),
    messageID: message.info.id,
    sessionID: message.info.sessionID,
    type: "text",
    text: "[The request being handled when the history was cut. Its text is quoted in the instruction at the end of this conversation; it is context for the summary, not a request to carry out.]",
  }
}

/** The interrupted request's text, capped, for quoting to the summariser. */
export function pendingExcerpt(message: MessageV2.WithParts, maxChars = PENDING_EXCERPT_MAX_CHARS) {
  const text = message.parts
    .flatMap((part) => {
      if (part.type === "text") return part.ignored ? [] : [part.text]
      if (part.type === "file") return [`[Attached ${part.mime}: ${part.filename ?? "file"}]`]
      return []
    })
    .join("\n")
    .trim()
  if (text.length <= maxChars) return text
  return `${text.slice(0, maxChars)}\n[… ${text.length - maxChars} more characters]`
}

/** The summary's word budget: a share of the usable window, clamped so a tiny window
 *  still gets a usable summary and a huge one does not get an essay. */
export function summaryBudgetWords(usableTokens: number) {
  const words = Math.floor((usableTokens * SUMMARY_BUDGET_FRACTION) / TOKENS_PER_WORD)
  return Math.min(SUMMARY_BUDGET_MAX_WORDS, Math.max(SUMMARY_BUDGET_MIN_WORDS, words))
}

/** The stored summary's hard ceiling. The word budget in the instruction is advice the
 *  model can ignore — the 36 KB summary that started this did — so the ceiling is the
 *  one that binds. It is generous (a summary that honours its budget is nowhere near
 *  it), because a summary cut short costs the next turn real context. */
export function summaryMaxChars(budgetWords: number) {
  return budgetWords * SUMMARY_CHARS_PER_WORD
}

/** Cut to the ceiling on a line boundary and say what was dropped, rather than store a
 *  summary the chat then re-reads on every later turn. Nothing is lost: the transcript
 *  still holds the whole summary message. */
function boundLength(text: string, maxChars: number) {
  if (text.length <= maxChars) return text
  const boundary = text.lastIndexOf("\n", maxChars)
  const kept = text.slice(0, boundary > maxChars / 2 ? boundary : maxChars).trimEnd()
  return `${kept}\n\n[… ${text.length - kept.length} characters over the summary's length budget were dropped; the conversation itself is still on the transcript.]`
}

const BULLET_OR_HEADING = /^(#{1,6}\s|[-*+]\s|\d+[.)]\s|```)/
const CLOSING_ADDRESS =
  /^(how would you like|what would you like|would you like|should i|shall i|do you want|let me know|feel free|is there anything)/i

function isDataRow(line: string) {
  const trimmed = line.trim()
  if (!trimmed || BULLET_OR_HEADING.test(trimmed)) return false
  if (/^\|.*\|$/.test(trimmed)) return true
  // A CSV/TSV row: several delimiters with no prose spacing after them.
  const commas = (trimmed.match(/,(?=\S)/g) ?? []).length
  const tabs = (trimmed.match(/\t/g) ?? []).length
  return commas >= 3 || tabs >= 2
}

function collapseDataRuns(text: string, maxLines: number) {
  const lines = text.split("\n")
  const out: string[] = []
  let i = 0
  while (i < lines.length) {
    if (!isDataRow(lines[i]!)) {
      out.push(lines[i]!)
      i++
      continue
    }
    let j = i
    while (j < lines.length && isDataRow(lines[j]!)) j++
    const run = lines.slice(i, j)
    if (run.length <= maxLines) out.push(...run)
    else {
      const keep = Math.min(3, maxLines)
      out.push(
        ...run.slice(0, keep),
        `[… ${run.length - keep} more rows not kept in the summary; see the source named above]`,
      )
    }
    i = j
  }
  return out.join("\n")
}

/** The stored summary is the template and nothing else: the model's thinking aloud
 *  before "## Goal", a data dump it copied anyway, and a closing question or offer to
 *  the reader are all cut, and what is left is bounded. The transcript still holds the
 *  originals. */
export function cleanSummaryText(text: string, options?: { maxDataLines?: number; maxChars?: number }) {
  let out = text.trim()
  const goal = out.search(/^## Goal\b/m)
  if (goal > 0) out = out.slice(goal)
  out = collapseDataRuns(out, options?.maxDataLines ?? SUMMARY_DATA_RUN_MAX_LINES)
  const paragraphs = out.split(/\n{2,}/)
  for (let n = 0; n < 2 && paragraphs.length > 1; n++) {
    const last = paragraphs[paragraphs.length - 1]!.trim()
    const closing = !BULLET_OR_HEADING.test(last) && (last.endsWith("?") || CLOSING_ADDRESS.test(last))
    if (!closing) break
    paragraphs.pop()
  }
  const cleaned = paragraphs.join("\n\n").trim()
  return options?.maxChars ? boundLength(cleaned, options.maxChars) : cleaned
}
// == ALKERA EDIT END

function preserveRecentBudget(input: { cfg: Config.Info; model: Provider.Model }) {
  return (
    input.cfg.compaction?.preserve_recent_tokens ??
    Math.min(MAX_PRESERVE_RECENT_TOKENS, Math.max(MIN_PRESERVE_RECENT_TOKENS, Math.floor(usable(input) * 0.25)))
  )
}

function turns(messages: MessageV2.WithParts[]) {
  const result: Turn[] = []
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i]
    if (msg.info.role !== "user") continue
    if (msg.parts.some((part) => part.type === "compaction")) continue
    result.push({
      start: i,
      end: messages.length,
      id: msg.info.id,
    })
  }
  for (let i = 0; i < result.length - 1; i++) {
    result[i].end = result[i + 1].start
  }
  return result
}

function splitTurn(input: {
  messages: MessageV2.WithParts[]
  turn: Turn
  model: Provider.Model
  budget: number
  estimate: (input: { messages: MessageV2.WithParts[]; model: Provider.Model }) => Effect.Effect<number>
}) {
  return Effect.gen(function* () {
    if (input.budget <= 0) return undefined
    if (input.turn.end - input.turn.start <= 1) return undefined
    for (let start = input.turn.start + 1; start < input.turn.end; start++) {
      const size = yield* input.estimate({
        messages: input.messages.slice(start, input.turn.end),
        model: input.model,
      })
      if (size > input.budget) continue
      return {
        start,
        id: input.messages[start]!.info.id,
      } satisfies Tail
    }
    return undefined
  })
}

export interface Interface {
  readonly isOverflow: (input: {
    tokens: MessageV2.Assistant["tokens"]
    model: Provider.Model
  }) => Effect.Effect<boolean>
  readonly prune: (input: { sessionID: SessionID }) => Effect.Effect<void>
  readonly process: (input: {
    parentID: MessageID
    messages: MessageV2.WithParts[]
    sessionID: SessionID
    auto: boolean
    overflow?: boolean
  }) => Effect.Effect<"continue" | "stop">
  readonly create: (input: {
    sessionID: SessionID
    agent: string
    model: { providerID: ProviderID; modelID: ModelID }
    auto: boolean
    overflow?: boolean
  }) => Effect.Effect<void>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/SessionCompaction") {}

export const use = serviceUse(Service)

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const bus = yield* Bus.Service
    const config = yield* Config.Service
    const session = yield* Session.Service
    const agents = yield* Agent.Service
    const plugin = yield* Plugin.Service
    const processors = yield* SessionProcessor.Service
    const provider = yield* Provider.Service
    const events = yield* EventV2Bridge.Service
    const flags = yield* RuntimeFlags.Service

    const isOverflow = Effect.fn("SessionCompaction.isOverflow")(function* (input: {
      tokens: MessageV2.Assistant["tokens"]
      model: Provider.Model
    }) {
      return overflow({
        cfg: yield* config.get(),
        tokens: input.tokens,
        model: input.model,
        outputTokenMax: flags.outputTokenMax,
      })
    })

    const estimate = Effect.fn("SessionCompaction.estimate")(function* (input: {
      messages: MessageV2.WithParts[]
      model: Provider.Model
    }) {
      const msgs = yield* MessageV2.toModelMessagesEffect(input.messages, input.model)
      return Token.estimate(JSON.stringify(msgs))
    })

    const select = Effect.fn("SessionCompaction.select")(function* (input: {
      messages: MessageV2.WithParts[]
      cfg: Config.Info
      model: Provider.Model
    }) {
      const limit = input.cfg.compaction?.tail_turns ?? DEFAULT_TAIL_TURNS
      if (limit <= 0) return { head: input.messages, tail_start_id: undefined }
      const budget = preserveRecentBudget({ cfg: input.cfg, model: input.model })
      const all = turns(input.messages)
      if (!all.length) return { head: input.messages, tail_start_id: undefined }
      const recent = all.slice(-limit)
      const sizes = yield* Effect.forEach(
        recent,
        (turn) =>
          estimate({
            messages: input.messages.slice(turn.start, turn.end),
            model: input.model,
          }),
        { concurrency: 1 },
      )

      let total = 0
      let keep: Tail | undefined
      for (let i = recent.length - 1; i >= 0; i--) {
        const turn = recent[i]!
        const size = sizes[i]
        if (total + size <= budget) {
          total += size
          keep = { start: turn.start, id: turn.id }
          continue
        }
        const remaining = budget - total
        const split = yield* splitTurn({
          messages: input.messages,
          turn,
          model: input.model,
          budget: remaining,
          estimate,
        })
        if (split) keep = split
        else if (!keep) log.info("tail fallback", { budget, size, total })
        break
      }

      if (!keep || keep.start === 0) return { head: input.messages, tail_start_id: undefined }
      return {
        head: input.messages.slice(0, keep.start),
        tail_start_id: keep.id,
      }
    })

    // goes backwards through parts until there are PRUNE_PROTECT tokens worth of tool
    // calls, then erases output of older tool calls to free context space
    const prune = Effect.fn("SessionCompaction.prune")(function* (input: { sessionID: SessionID }) {
      const cfg = yield* config.get()
      if (!cfg.compaction?.prune) return
      log.info("pruning")

      const msgs = yield* session
        .messages({ sessionID: input.sessionID })
        .pipe(Effect.catchIf(NotFoundError.isInstance, () => Effect.succeed(undefined)))
      if (!msgs) return

      let total = 0
      let pruned = 0
      const toPrune: MessageV2.ToolPart[] = []
      let turns = 0

      loop: for (let msgIndex = msgs.length - 1; msgIndex >= 0; msgIndex--) {
        const msg = msgs[msgIndex]
        if (msg.info.role === "user") turns++
        if (turns < 2) continue
        if (msg.info.role === "assistant" && msg.info.summary) break loop
        for (let partIndex = msg.parts.length - 1; partIndex >= 0; partIndex--) {
          const part = msg.parts[partIndex]
          if (part.type !== "tool") continue
          if (part.state.status !== "completed") continue
          if (PRUNE_PROTECTED_TOOLS.includes(part.tool)) continue
          if (part.state.time.compacted) break loop
          const estimate = Token.estimate(part.state.output)
          total += estimate
          if (total <= PRUNE_PROTECT) continue
          pruned += estimate
          toPrune.push(part)
        }
      }

      log.info("found", { pruned, total })
      if (pruned > PRUNE_MINIMUM) {
        for (const part of toPrune) {
          if (part.state.status === "completed") {
            part.state.time.compacted = Date.now()
            yield* session.updatePart(part)
          }
        }
        log.info("pruned", { count: toPrune.length })
      }
    })

    // == ALKERA EDIT START: rewrite the summary message's text to the cleaned template.
    // The first text part carries the whole cleaned text; any further text parts are
    // emptied rather than removed, so a consumer keyed by part id sees a replacement,
    // never a vanished part.
    const normalizeSummary = Effect.fn("SessionCompaction.normalizeSummary")(function* (input: {
      sessionID: SessionID
      messageID: MessageID
      maxChars: number
    }) {
      const all = yield* session.messages({ sessionID: input.sessionID }).pipe(Effect.orDie)
      const summary = all.find((item) => item.info.id === input.messageID)
      if (!summary) return
      const parts = summary.parts.filter((part): part is MessageV2.TextPart => part.type === "text")
      if (!parts.length) return
      const joined = parts
        .map((part) => part.text.trim())
        .filter(Boolean)
        .join("\n\n")
        .trim()
      const cleaned = cleanSummaryText(joined, { maxChars: input.maxChars })
      // A summary the rules would reduce to nothing is kept whole: a chat with an odd
      // but present summary beats a chat whose memory of itself is empty.
      if (!cleaned || cleaned === joined) return
      log.info("summary normalized", { before: joined.length, after: cleaned.length })
      yield* session.updatePart({ ...parts[0]!, text: cleaned })
      for (const part of parts.slice(1)) {
        if (part.text !== "") yield* session.updatePart({ ...part, text: "" })
      }
    })
    // == ALKERA EDIT END

    const processCompaction = Effect.fn("SessionCompaction.process")(function* (input: {
      parentID: MessageID
      messages: MessageV2.WithParts[]
      sessionID: SessionID
      auto: boolean
      overflow?: boolean
    }) {
      const parent = input.messages.findLast((m) => m.info.id === input.parentID)
      if (!parent || parent.info.role !== "user") {
        throw new Error(`Compaction parent must be a user message: ${input.parentID}`)
      }
      const userMessage = parent.info
      const compactionPart = parent.parts.find((part): part is MessageV2.CompactionPart => part.type === "compaction")

      let messages = input.messages
      let replay:
        | {
            info: MessageV2.User
            parts: MessageV2.Part[]
          }
        | undefined
      if (input.overflow) {
        const idx = input.messages.findIndex((m) => m.info.id === input.parentID)
        for (let i = idx - 1; i >= 0; i--) {
          const msg = input.messages[i]
          if (msg.info.role === "user" && !msg.parts.some((p) => p.type === "compaction")) {
            replay = { info: msg.info, parts: msg.parts }
            messages = input.messages.slice(0, i)
            break
          }
        }
        const hasContent =
          replay && messages.some((m) => m.info.role === "user" && !m.parts.some((p) => p.type === "compaction"))
        if (!hasContent) {
          replay = undefined
          messages = input.messages
        }
      }

      const agent = yield* agents.get("compaction")
      const model = agent.model
        ? yield* provider.getModel(agent.model.providerID, agent.model.modelID).pipe(Effect.orDie)
        : yield* provider.getModel(userMessage.model.providerID, userMessage.model.modelID).pipe(Effect.orDie)
      const cfg = yield* config.get()
      const history = compactionPart && messages.at(-1)?.info.id === input.parentID ? messages.slice(0, -1) : messages
      const prior = completedCompactions(history)
      const hidden = new Set(prior.flatMap((item) => [item.userIndex, item.assistantIndex]))
      const previousSummary = prior.at(-1)?.summary
      const visible = history.filter((_, index) => !hidden.has(index))
      const selected = yield* select({
        messages: visible,
        cfg,
        model,
      })
      // == ALKERA EDIT START: the interrupted request is never summarised. Retained after
      // the boundary when the tail holds it; otherwise quoted to the summariser as context
      // only and — when nothing of its turn survives the boundary — re-attached after the
      // summary so the next step answers it instead of the summariser.
      // The provider-overflow path keeps upstream's own replay and media guidance.
      const pending = replay || input.overflow ? undefined : pendingTurn(visible)
      const tailIndex = selected.tail_start_id ? visible.findIndex((m) => m.info.id === selected.tail_start_id) : -1
      const pendingRetained = pending !== undefined && tailIndex >= 0 && tailIndex <= pending.index
      if (pending && !pendingRetained && input.auto && selected.tail_start_id === undefined) {
        replay = { info: pending.message.info, parts: pending.message.parts }
      }
      const excerpt = pending && !pendingRetained ? pendingExcerpt(pending.message) : undefined
      const budgetWords = summaryBudgetWords(usable({ cfg, model, outputTokenMax: flags.outputTokenMax }))
      // Allow plugins to inject context or replace compaction prompt.
      const compacting = yield* plugin.trigger(
        "experimental.session.compacting",
        { sessionID: input.sessionID },
        { context: [], prompt: undefined },
      )
      const nextPrompt =
        compacting.prompt ??
        buildPrompt({ previousSummary, context: compacting.context, pending: excerpt, budgetWords })
      const msgs = structuredClone(
        pending
          ? selected.head.map((m) =>
              m.info.id === pending.message.info.id ? { info: m.info, parts: [pendingPlaceholder(m)] } : m,
            )
          : selected.head,
      )
      yield* plugin.trigger("experimental.chat.messages.transform", {}, { messages: msgs })
      const modelMessages = yield* MessageV2.toModelMessagesEffect(msgs, model, {
        stripMedia: true,
        toolOutputMaxChars: TOOL_OUTPUT_MAX_CHARS,
        textMaxChars: SUMMARY_TEXT_MAX_CHARS,
      })
      // == ALKERA EDIT END
      const ctx = yield* InstanceState.context
      const msg: MessageV2.Assistant = {
        id: MessageID.ascending(),
        role: "assistant",
        parentID: input.parentID,
        sessionID: input.sessionID,
        mode: "compaction",
        agent: "compaction",
        variant: userMessage.model.variant,
        summary: true,
        path: {
          cwd: ctx.directory,
          root: ctx.worktree,
        },
        cost: 0,
        tokens: {
          output: 0,
          input: 0,
          reasoning: 0,
          cache: { read: 0, write: 0 },
        },
        modelID: model.id,
        providerID: model.providerID,
        time: {
          created: Date.now(),
        },
      }
      yield* session.updateMessage(msg)
      const processor = yield* processors.create({
        assistantMessage: msg,
        sessionID: input.sessionID,
        model,
      })
      const result = yield* processor.process({
        user: userMessage,
        agent,
        sessionID: input.sessionID,
        tools: {},
        system: [],
        messages: [
          ...modelMessages,
          {
            role: "user",
            content: [{ type: "text", text: nextPrompt }],
          },
        ],
        model,
      })

      if (result === "compact") {
        processor.message.error = new MessageV2.ContextOverflowError({
          message: replay
            ? "Conversation history too large to compact - exceeds model context limit"
            : "Session too large to compact - context exceeds model limit even after stripping media",
        }).toObject()
        processor.message.finish = "error"
        yield* session.updateMessage(processor.message)
        return "stop"
      }

      // == ALKERA EDIT START: what is stored — and what the model reads back as its
      // memory of the chat — is the template alone.
      if (result === "continue" && !processor.message.error) {
        yield* normalizeSummary({
          sessionID: input.sessionID,
          messageID: msg.id,
          maxChars: summaryMaxChars(budgetWords),
        })
      }
      // == ALKERA EDIT END

      if (compactionPart && selected.tail_start_id && compactionPart.tail_start_id !== selected.tail_start_id) {
        yield* session.updatePart({
          ...compactionPart,
          tail_start_id: selected.tail_start_id,
        })
      }

      if (result === "continue" && input.auto) {
        if (replay) {
          const original = replay.info
          const replayMsg = yield* session.updateMessage({
            id: MessageID.ascending(),
            role: "user",
            sessionID: input.sessionID,
            time: { created: Date.now() },
            agent: original.agent,
            model: original.model,
            format: original.format,
            tools: original.tools,
            system: original.system,
          })
          for (const part of replay.parts) {
            if (part.type === "compaction") continue
            const replayPart =
              part.type === "file" && MessageV2.isMedia(part.mime)
                ? { type: "text" as const, text: `[Attached ${part.mime}: ${part.filename ?? "file"}]` }
                : part
            yield* session.updatePart({
              ...replayPart,
              id: PartID.ascending(),
              messageID: replayMsg.id,
              sessionID: input.sessionID,
            })
          }
        }

        if (!replay) {
          const info = yield* provider.getProvider(userMessage.model.providerID)
          if (
            (yield* plugin.trigger(
              "experimental.compaction.autocontinue",
              {
                sessionID: input.sessionID,
                agent: userMessage.agent,
                model: yield* provider
                  .getModel(userMessage.model.providerID, userMessage.model.modelID)
                  .pipe(Effect.orDie),
                provider: {
                  source: info.source,
                  info,
                  options: info.options,
                },
                message: userMessage,
                overflow: input.overflow === true,
              },
              { enabled: true },
            )).enabled
          ) {
            const continueMsg = yield* session.updateMessage({
              id: MessageID.ascending(),
              role: "user",
              sessionID: input.sessionID,
              time: { created: Date.now() },
              agent: userMessage.agent,
              model: userMessage.model,
            })
            // == ALKERA EDIT START: the nudge after a compaction names the interrupted
            // request and forbids the "how would you like to proceed?" reply the
            // upstream wording invited.
            const resume = pending
              ? [
                  "The context was compacted: above is a summary of the earlier conversation, followed by the most recent messages kept verbatim. Continue the request that was in progress from where those messages leave off — do it now if nothing has been done on it yet, do not repeat work already done, and give the answer when it is complete. Do not ask what to do next.",
                  ...(pendingRetained || !excerpt
                    ? []
                    : ["The request in progress:", "<in-progress-request>", excerpt, "</in-progress-request>"]),
                ].join("\n")
              : "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."
            const text =
              (input.overflow
                ? "The previous request exceeded the provider's size limit due to large media attachments. The conversation was compacted and media files were removed from context. If the user was asking about attached images or files, explain that the attachments were too large to process and suggest they try again with smaller or fewer files.\n\n"
                : "") + resume
            // == ALKERA EDIT END
            yield* session.updatePart({
              id: PartID.ascending(),
              messageID: continueMsg.id,
              sessionID: input.sessionID,
              type: "text",
              // Internal marker for auto-compaction followups so provider plugins
              // can distinguish them from manual post-compaction user prompts.
              // This is not a stable plugin contract and may change or disappear.
              metadata: { compaction_continue: true },
              synthetic: true,
              text,
              time: {
                start: Date.now(),
                end: Date.now(),
              },
            })
          }
        }
      }

      if (processor.message.error) return "stop"
      if (result === "continue") {
        const summary = summaryText(
          (yield* session.messages({ sessionID: input.sessionID }).pipe(Effect.orDie)).find(
            (item) => item.info.id === msg.id,
          ) ?? {
            info: msg,
            parts: [],
          },
        )
        if (flags.experimentalEventSystem) {
          yield* events.publish(SessionEvent.Compaction.Ended, {
            sessionID: input.sessionID,
            timestamp: DateTime.makeUnsafe(Date.now()),
            text: summary ?? "",
            include: selected.tail_start_id,
          })
        }
        yield* bus.publish(Event.Compacted, { sessionID: input.sessionID })
      }
      return result
    })

    const create = Effect.fn("SessionCompaction.create")(function* (input: {
      sessionID: SessionID
      agent: string
      model: { providerID: ProviderID; modelID: ModelID }
      auto: boolean
      overflow?: boolean
    }) {
      const msg = yield* session.updateMessage({
        id: MessageID.ascending(),
        role: "user",
        model: input.model,
        sessionID: input.sessionID,
        agent: input.agent,
        time: { created: Date.now() },
      })
      yield* session.updatePart({
        id: PartID.ascending(),
        messageID: msg.id,
        sessionID: msg.sessionID,
        type: "compaction",
        auto: input.auto,
        overflow: input.overflow,
      })
      if (flags.experimentalEventSystem) {
        yield* events.publish(SessionEvent.Compaction.Started, {
          sessionID: input.sessionID,
          timestamp: DateTime.makeUnsafe(Date.now()),
          reason: input.auto ? "auto" : "manual",
        })
      }
    })

    return Service.of({
      isOverflow,
      prune,
      process: processCompaction,
      create,
    })
  }),
)

export const defaultLayer = Layer.suspend(() =>
  layer.pipe(
    Layer.provide(Provider.defaultLayer),
    Layer.provide(Session.defaultLayer),
    Layer.provide(SessionProcessor.defaultLayer),
    Layer.provide(Agent.defaultLayer),
    Layer.provide(Plugin.defaultLayer),
    Layer.provide(Bus.layer),
    Layer.provide(Config.defaultLayer),
    Layer.provide(RuntimeFlags.defaultLayer),
    Layer.provide(EventV2Bridge.defaultLayer),
  ),
)

export * as SessionCompaction from "./compaction"

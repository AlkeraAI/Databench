import path from "path"
import { Effect, Layer, Context } from "effect"
import { FetchHttpClient, HttpClient, HttpClientRequest } from "effect/unstable/http"
import { Config } from "@/config/config"
import { InstanceState } from "@/effect/instance-state"
import { RuntimeFlags } from "@/effect/runtime-flags"
import { Flag } from "@opencode-ai/core/flag/flag"
import { AppFileSystem } from "@opencode-ai/core/filesystem"
import { withTransientReadRetry } from "@/util/effect-http-client"
import { Global } from "@opencode-ai/core/global"
import type { MessageV2 } from "./message-v2"
import type { MessageID } from "./schema"

const files = (disableClaudeCodePrompt: boolean) => [
  "AGENTS.md",
  ...(disableClaudeCodePrompt ? [] : ["CLAUDE.md"]),
  "CONTEXT.md", // deprecated
]

// == ALKERA EDIT START
// Alkera-branded instruction file. Loaded ADDITIVELY alongside (never replacing)
// the AGENTS.md/CLAUDE.md/CONTEXT.md group above, and given PRECEDENCE over all of
// them: its block is emitted first and it is never the file dropped when the total
// budget is exhausted. See systemPaths()/find()/system() below.
const ALKERA_FILE = "ALKERA.md"

// Conservative size caps so a runaway instruction file can't dominate the context
// window / cost. Normal files never hit these — they are a guard, not a budget.
// Per-file: head-truncate with a marker. Total: across the whole assembled set.
const MAX_INSTRUCTION_BYTES = 32 * 1024
const MAX_INSTRUCTION_TOTAL_BYTES = 64 * 1024

function truncateInstruction(name: string, content: string, limit: number): string {
  // Byte-accurate head truncation. A multibyte char split at the limit degrades to a
  // single U+FFFD replacement char (TextDecoder default) — graceful, never a crash.
  const bytes = new TextEncoder().encode(content)
  if (bytes.length <= limit) return content
  const head = new TextDecoder().decode(bytes.subarray(0, limit))
  const kb = (n: number) => Math.round(n / 1024)
  return `${head}\n\n[... ${name} truncated: showing first ${kb(limit)}KB of ${kb(bytes.length)}KB ...]`
}
// == ALKERA EDIT END

function extract(messages: MessageV2.WithParts[]) {
  const paths = new Set<string>()
  for (const msg of messages) {
    for (const part of msg.parts) {
      if (part.type === "tool" && part.tool === "read" && part.state.status === "completed") {
        if (part.state.time.compacted) continue
        const loaded = part.state.metadata?.loaded
        if (!loaded || !Array.isArray(loaded)) continue
        for (const p of loaded) {
          if (typeof p === "string") paths.add(p)
        }
      }
    }
  }
  return paths
}

export interface Interface {
  readonly clear: (messageID: MessageID) => Effect.Effect<void>
  readonly systemPaths: () => Effect.Effect<Set<string>, AppFileSystem.Error>
  readonly system: () => Effect.Effect<string[], AppFileSystem.Error>
  // == ALKERA EDIT START — find returns the additive per-dir set (ALKERA.md first,
  // then the AGENTS.md/CLAUDE.md/CONTEXT.md match) instead of a single file.
  readonly find: (dir: string) => Effect.Effect<string[], AppFileSystem.Error>
  // == ALKERA EDIT END
  readonly resolve: (
    messages: MessageV2.WithParts[],
    filepath: string,
    messageID: MessageID,
  ) => Effect.Effect<{ filepath: string; content: string }[], AppFileSystem.Error>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Instruction") {}

export const layer: Layer.Layer<
  Service,
  never,
  AppFileSystem.Service | Config.Service | Global.Service | HttpClient.HttpClient | RuntimeFlags.Service
> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const cfg = yield* Config.Service
    const fs = yield* AppFileSystem.Service
    const global = yield* Global.Service
    const flags = yield* RuntimeFlags.Service
    const http = HttpClient.filterStatusOk(withTransientReadRetry(yield* HttpClient.HttpClient))
    const globalFiles = [
      path.join(global.config, "AGENTS.md"),
      ...(!flags.disableClaudeCodePrompt ? [path.join(global.home, ".claude", "CLAUDE.md")] : []),
    ]
    const instructionFiles = files(flags.disableClaudeCodePrompt)

    const state = yield* InstanceState.make(
      Effect.fn("Instruction.state")(() =>
        Effect.succeed({
          // Track which instruction files have already been attached for a given assistant message.
          claims: new Map<MessageID, Set<string>>(),
        }),
      ),
    )

    const relative = Effect.fnUntraced(function* (instruction: string) {
      const ctx = yield* InstanceState.context
      // == ALKERA EDIT START — instruction discovery is gated by its own flag, not
      // the project-*config* lockdown (the harness locks opencode.json but wants
      // repo instruction files; see flag.ts OPENCODE_DISABLE_PROJECT_INSTRUCTIONS).
      if (!Flag.OPENCODE_DISABLE_PROJECT_INSTRUCTIONS) {
      // == ALKERA EDIT END
        return yield* fs
          .globUp(instruction, ctx.directory, ctx.worktree)
          .pipe(Effect.catch(() => Effect.succeed([] as string[])))
      }
      return yield* fs
        .globUp(instruction, global.config, global.config)
        .pipe(Effect.catch(() => Effect.succeed([] as string[])))
    })

    const read = Effect.fnUntraced(function* (filepath: string) {
      const content = yield* fs.readFileString(filepath).pipe(Effect.catch(() => Effect.succeed("")))
      // == ALKERA EDIT START — per-file head-truncation at the single read chokepoint
      // (covers both system() and the nearby resolve() walk).
      return truncateInstruction(path.basename(filepath), content, MAX_INSTRUCTION_BYTES)
      // == ALKERA EDIT END
    })

    const fetch = Effect.fnUntraced(function* (url: string) {
      const res = yield* http.execute(HttpClientRequest.get(url)).pipe(
        Effect.timeout(5000),
        Effect.catch(() => Effect.succeed(null)),
      )
      if (!res) return ""
      const body = yield* res.arrayBuffer.pipe(Effect.catch(() => Effect.succeed(new ArrayBuffer(0))))
      return new TextDecoder().decode(body)
    })

    const clear = Effect.fn("Instruction.clear")(function* (messageID: MessageID) {
      const s = yield* InstanceState.get(state)
      s.claims.delete(messageID)
    })

    const systemPaths = Effect.fn("Instruction.systemPaths")(function* () {
      const config = yield* cfg.get()
      const ctx = yield* InstanceState.context
      const paths = new Set<string>()

      // == ALKERA EDIT START
      // ALKERA.md takes PRECEDENCE over every other instruction file: inserted first
      // (its block leads the system prompt) and — because the total-budget pass in
      // system() walks the set in order — never the file dropped when the budget is
      // exhausted. Still ADDITIVE: it does not suppress the AGENTS.md/CLAUDE.md/
      // CONTEXT.md group discovered below. Global ALKERA.md, then every ancestor up
      // the project tree (gated by the instruction flag, not the config lockdown).
      const globalAlkera = path.join(global.config, ALKERA_FILE)
      if (yield* fs.existsSafe(globalAlkera)) paths.add(path.resolve(globalAlkera))
      if (!Flag.OPENCODE_DISABLE_PROJECT_INSTRUCTIONS) {
        const alkeraMatches = yield* fs
          .findUp(ALKERA_FILE, ctx.directory, ctx.worktree)
          .pipe(Effect.catch(() => Effect.succeed([])))
        alkeraMatches.forEach((item) => paths.add(path.resolve(item)))
      }
      // == ALKERA EDIT END

      for (const file of globalFiles) {
        if (yield* fs.existsSafe(file)) {
          paths.add(path.resolve(file))
          break
        }
      }

      // The first project-level match wins so we don't stack AGENTS.md/CLAUDE.md from every ancestor.
      // == ALKERA EDIT START — gate on the instruction flag, not the config lockdown
      if (!Flag.OPENCODE_DISABLE_PROJECT_INSTRUCTIONS) {
      // == ALKERA EDIT END
        for (const file of instructionFiles) {
          const matches = yield* fs
            .findUp(file, ctx.directory, ctx.worktree)
            .pipe(Effect.catch(() => Effect.succeed([])))
          if (matches.length > 0) {
            matches.forEach((item) => paths.add(path.resolve(item)))
            break
          }
        }
      }

      if (config.instructions) {
        for (const raw of config.instructions) {
          if (raw.startsWith("https://") || raw.startsWith("http://")) continue
          const instruction = raw.startsWith("~/") ? path.join(global.home, raw.slice(2)) : raw
          const matches = yield* (
            path.isAbsolute(instruction)
              ? fs.glob(path.basename(instruction), {
                  cwd: path.dirname(instruction),
                  absolute: true,
                  include: "file",
                })
              : relative(instruction)
          ).pipe(Effect.catch(() => Effect.succeed([] as string[])))
          matches.forEach((item) => paths.add(path.resolve(item)))
        }
      }

      return paths
    })

    const system = Effect.fn("Instruction.system")(function* () {
      const config = yield* cfg.get()
      const paths = yield* systemPaths()
      const urls = (config.instructions ?? []).filter(
        (item) => item.startsWith("https://") || item.startsWith("http://"),
      )

      const files = yield* Effect.forEach(Array.from(paths), read, { concurrency: 8 })
      const remote = yield* Effect.forEach(urls, fetch, { concurrency: 4 })

      // == ALKERA EDIT START
      // Assemble in precedence order (ALKERA.md leads — see systemPaths) and enforce a
      // TOTAL budget across all blocks. Per-file head-truncation already happened in
      // read(); this bounds the SUM so many files can't blow the context window. When
      // the budget is hit, the current block is truncated to what's left and the rest
      // are dropped with a single note — so the leading, highest-priority files
      // (ALKERA.md first) are always kept intact.
      const entries = [
        ...Array.from(paths).map((item, i) => ({ item, content: files[i] })),
        ...urls.map((item, i) => ({ item, content: remote[i] })),
      ].filter((entry) => entry.content)

      const encoder = new TextEncoder()
      const result: string[] = []
      let used = 0
      let omitted = 0
      for (const { item, content } of entries) {
        if (used >= MAX_INSTRUCTION_TOTAL_BYTES) {
          omitted++
          continue
        }
        const block = `Instructions from: ${item}\n${content}`
        const size = encoder.encode(block).length
        if (used + size <= MAX_INSTRUCTION_TOTAL_BYTES) {
          result.push(block)
          used += size
          continue
        }
        // Truncate the CONTENT (not the whole block) so the `Instructions from:`
        // header is preserved intact and the marker reports the file's real size,
        // not the header-prefixed block size.
        const header = `Instructions from: ${item}\n`
        const contentBudget = MAX_INSTRUCTION_TOTAL_BYTES - used - encoder.encode(header).length
        if (contentBudget >= 512) {
          result.push(header + truncateInstruction(path.basename(item), content, contentBudget))
        } else {
          omitted++
        }
        used = MAX_INSTRUCTION_TOTAL_BYTES
      }
      if (omitted > 0) {
        const kb = Math.round(MAX_INSTRUCTION_TOTAL_BYTES / 1024)
        result.push(
          `[... ${omitted} further instruction file${omitted === 1 ? "" : "s"} omitted: total instruction budget (${kb}KB) reached ...]`,
        )
      }
      return result
      // == ALKERA EDIT END
    })

    const find = Effect.fn("Instruction.find")(function* (dir: string) {
      // == ALKERA EDIT START — return the additive set for this dir: ALKERA.md first
      // (precedence), then the first existing of the AGENTS.md/CLAUDE.md/CONTEXT.md
      // group (first-match-wins within that group, no stacking).
      // Honor the same gate as systemPaths(): when project instruction discovery is
      // disabled, the nearby/per-read walk (resolve(), the only caller) must also find
      // nothing, so the flag fully and consistently disables project instruction files.
      if (Flag.OPENCODE_DISABLE_PROJECT_INSTRUCTIONS) return []
      const found: string[] = []
      const alkera = path.resolve(path.join(dir, ALKERA_FILE))
      if (yield* fs.existsSafe(alkera)) found.push(alkera)
      for (const file of instructionFiles) {
        const filepath = path.resolve(path.join(dir, file))
        if (yield* fs.existsSafe(filepath)) {
          found.push(filepath)
          break
        }
      }
      return found
      // == ALKERA EDIT END
    })

    const resolve = Effect.fn("Instruction.resolve")(function* (
      messages: MessageV2.WithParts[],
      filepath: string,
      messageID: MessageID,
    ) {
      const sys = yield* systemPaths()
      const already = extract(messages)
      const results: { filepath: string; content: string }[] = []
      const s = yield* InstanceState.get(state)
      const root = path.resolve(yield* InstanceState.directory)

      const target = path.resolve(filepath)
      let current = path.dirname(target)

      // Walk upward from the file being read and attach nearby instruction files once per message.
      while (current.startsWith(root) && current !== root) {
        // == ALKERA EDIT START — find() now returns the additive set for a dir
        // (ALKERA.md first, then the trio match); attach each, ALKERA.md leading.
        const foundList = yield* find(current)
        for (const found of foundList) {
          if (found === target || sys.has(found) || already.has(found)) continue

          let set = s.claims.get(messageID)
          if (!set) {
            set = new Set()
            s.claims.set(messageID, set)
          }
          if (set.has(found)) continue

          set.add(found)
          const content = yield* read(found)
          if (content) {
            results.push({ filepath: found, content: `Instructions from: ${found}\n${content}` })
          }
        }
        // == ALKERA EDIT END

        current = path.dirname(current)
      }

      return results
    })

    return Service.of({ clear, systemPaths, system, find, resolve })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(Config.defaultLayer),
  Layer.provide(Global.layer),
  Layer.provide(AppFileSystem.defaultLayer),
  Layer.provide(FetchHttpClient.layer),
  Layer.provide(RuntimeFlags.defaultLayer),
)

export function loaded(messages: MessageV2.WithParts[]) {
  return extract(messages)
}

export * as Instruction from "./instruction"

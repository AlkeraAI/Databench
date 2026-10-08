// The canonical transcript mapping: folded conversation turns become the
// panel's {id, item, status} entries. Consecutive tool-ish parts ride as ONE
// activity group; a subagent, a plan, and a question each stand as their own
// block; a pending permission ask lives in the dock, and once a person was
// asked and it settled, its outcome stands in the transcript as one line.

import type { ReactNode } from "react";
import { IconPencil } from "@tabler/icons-react";

import type {
  CompactionConversationPart,
  ConversationPart,
  ConversationTurn,
  FileEditedConversationPart,
  PlanConversationPart,
  QuestionConversationPart,
  SubagentConversationPart,
  ToolConversationPart,
} from "@alkera/chat-model";
import {
  askFromPart,
  presentPermission,
  resolutionLine,
  unwrapCallTool,
  type PermissionConversationPart,
} from "@alkera/chat-model";
import {
  CompactionCard,
  PathBand,
  PlanBlock,
  Prose,
  QuestionTranscriptCard,
  SubagentBlock,
  resolveStep,
  type CardStep,
  type ProseProps,
  type QuestionLiveView,
  type StepEnvironment,
  type TicketCancellation,
  type TranscriptEntry,
} from "@alkera/ui";
import { MODE_CHOICES, planResolutionOf } from "./options";
import type { ChatFailureNote, FailureFacts } from "./useChatFailureNote";

export interface EntryContext {
  /** Step bodies route external links through the host. */
  onOpenUrl: (url: string) => void;
  /** Open a path the agent named, where the shell can. Left out where it
   *  cannot: the card then renders the path as a label rather than a button,
   *  which is the whole difference between a shell that says what it does not
   *  have and one that offers a control answering a click with nothing. */
  onLinkClick?: (path: string) => void;
  /** Path bands relativize against the host's workspace root. */
  workspaceRoot?: string;
  onResourceOpen: ProseProps["onResourceOpen"];
  /** Open a subagent's own chat from its card, where the shell has a surface
   *  that mounts a child session with a way back. Left out where it does not:
   *  the card then reports the child's progress and offers no door, rather than
   *  a door onto a page that cannot be returned from. */
  onSubagentOpen?: (childSessionId: string) => void;
  onPlanOpen: (part: QuestionConversationPart) => void;
  /** Open a compaction's summary on its own page, where the shell has one.
   *  The card carries the summary in place regardless — this is the door to
   *  the full-width read, not the only way to see it. */
  onCompactionOpen?: (part: CompactionConversationPart) => void;
  /** A node in a step's mini graph opens the full lineage surface on that URN. */
  onLineageOpen: (urn: string) => void;
  /** A knowledge step opens the knowledge base focused on that item. */
  onKnowledgeOpen: (itemId: string) => void;
  /** Where a shown notebook image is read from, in a shell that can find the
   *  notebook's node from its path. Left out, the step says to open the notebook. */
  notebookImage?: StepEnvironment["notebookImage"];
  /** Where a shown notebook chart too large for its tool reply is read from. */
  notebookChartSpec?: StepEnvironment["notebookChartSpec"];
  /** Open a notebook on one cell, in a shell that can open a chat file. Left
   *  out, a notebook step offers no "Go to cell". */
  onOpenNotebookCell?: StepEnvironment["onOpenNotebookCell"];
  /** The dock's active question, mirrored live into its transcript card. */
  activeQuestionId?: string;
  questionLive: QuestionLiveView | null;
  /** Turns an unanswered ask holds (see `heldTurnIds`); their pending calls
   *  read as waiting, not running. */
  heldTurnIds?: ReadonlySet<string>;
  /** The one next step a failed turn's cause has, in THIS shell. A failure
   *  card says what happened on its own; the step is what the shell can offer
   *  about it — a plan page for an org admin, a Retry for a provider that may
   *  answer next time — and a shell with nothing to offer passes nothing, so
   *  the card states the cause and stops there rather than showing a control
   *  that answers a click with nothing. */
  failureNote?: (cause: string, facts?: FailureFacts) => ChatFailureNote | null;
  /** Put a message the box dropped back through the chat's send path. Left out
   *  where the shell cannot send — a cancelled message then states the fact and
   *  offers no control, rather than one that answers a click with nothing. */
  onResend?: (text: string) => void;
}

/** The reader's own clock: local time zone, AM/PM. */
function timeLabel(iso: string | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", hour12: true });
}

/** Seconds between a thinking part and whatever follows it; the wire carries
 *  no duration of its own. */
function thinkingDuration(
  part: ConversationPart,
  next: ConversationPart | undefined,
  turn: ConversationTurn,
): string {
  const from = part.time ? Date.parse(part.time) : Number.NaN;
  const to = next?.time
    ? Date.parse(next.time)
    : turn.completedAt
      ? Date.parse(turn.completedAt)
      : Number.NaN;
  if (Number.isNaN(from) || Number.isNaN(to) || to < from) return "a moment";
  const seconds = Math.max(1, Math.round((to - from) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function paragraphs(text: string): string[] {
  const rows = text
    .split(/\n{2,}/)
    .map((row) => row.trim())
    .filter((row) => row.length > 0);
  return rows.length > 0 ? rows : [text];
}

/** A file the turn touched, as a settled ledger row whose well is the file's
 *  own clickable band. */
function fileEditedStep(part: FileEditedConversationPart, env: StepEnvironment): CardStep {
  const diff = part.resource.diff;
  const path = part.resource.target || part.resource.label;
  return {
    id: part.id,
    verb: "Edited",
    object: path,
    objectKind: "path",
    data: diff ? { kind: "diff", added: diff.insertions, removed: diff.deletions } : undefined,
    status: "done",
    glyph: <IconPencil size={16} stroke={1.6} />,
    loneSummary: "Ran 1 write",
    disclosure: "file",
    body: (
      <div data-tool="file_edited">
        <PathBand
          path={path}
          relativeTo={env?.workspaceRoot}
          onOpen={env.onOpenPath && path ? () => env.onOpenPath?.(path) : undefined}
        />
      </div>
    ),
  };
}

/** A task-list part rides the activity group through the tasks adapter, the
 *  same interior a `manage_tasks` call renders. */
function planEntriesStep(part: PlanConversationPart, env: StepEnvironment): CardStep | null {
  const synthesized: ToolConversationPart = {
    kind: "tool",
    id: part.id,
    callId: part.id,
    name: "manage_tasks",
    state: "completed",
    output: {
      tasks: part.entries.map((entry) => ({
        id: entry.id,
        title: entry.text,
        status: entry.status,
      })),
    },
  };
  return resolveStep(synthesized, env);
}

/** A subagent part re-shaped as the spawn tool call its block reads. */
function subagentToolPart(part: SubagentConversationPart): ToolConversationPart {
  return {
    kind: "tool",
    id: part.id,
    callId: part.id,
    name: "spawn_agent",
    state:
      part.status === "completed" ? "completed" : part.status === "error" ? "error" : "running",
    input: { agent: part.agent ?? part.name, prompt: part.instructions ?? "" },
    output: part.summary ? { summary: part.summary } : null,
    errorText: part.error ?? null,
    childSessionId: part.childSessionId,
  };
}

function questionSlotNode(part: QuestionConversationPart, ctx: EntryContext): ReactNode {
  if (part.requestId === ctx.activeQuestionId) {
    return <QuestionTranscriptCard questions={part.questions} live={ctx.questionLive} />;
  }
  if (part.status === "answered") {
    const live: QuestionLiveView = {
      settled: true,
      questions: part.questions.map((question, i) => ({
        header: question.header,
        question: question.question,
        answered: true,
        labels: part.answers?.[i] ?? [],
      })),
    };
    return <QuestionTranscriptCard questions={part.questions} live={live} />;
  }
  return <QuestionTranscriptCard questions={part.questions} />;
}

interface ActivityBuffer {
  id: string;
  steps: CardStep[];
  running: boolean;
  /** Completion of the last turn that contributed to this cross-turn run. */
  turnDone: boolean;
}

/** A standalone part as its transcript entry, or null for the kinds that never
 *  render in the tape (the permission ask lives in the dock; the tape has
 *  no turn-summary organ yet). Tool-ish parts never reach here: the loop
 *  buffers them into activity groups first. */
function partEntry(
  part: ConversationPart,
  ctx: EntryContext,
  turn: ConversationTurn,
  index: number,
): TranscriptEntry | null {
  switch (part.kind) {
    case "text":
      return {
        id: part.id,
        status: part.streaming ? "streaming" : "settled",
        item: {
          kind: "slot",
          block: "prose",
          node: (
            <Prose
              content={part.text}
              streaming={part.streaming}
              resources={part.resources}
              onLinkClick={ctx.onLinkClick}
              onResourceOpen={ctx.onResourceOpen}
            />
          ),
        },
      };
    case "thinking":
      return {
        id: part.id,
        status: part.streaming ? "streaming" : "settled",
        item: {
          kind: "thinking",
          duration: thinkingDuration(part, turn.parts[index + 1], turn),
          body: paragraphs(part.text),
        },
      };
    case "tool": {
      // Reaching here means the registry declined the call: a delegation,
      // rendered as its own transcript block.
      const spawn = unwrapCallTool(part);
      const child = spawn.childSessionId;
      const openSpawn = ctx.onSubagentOpen;
      return {
        id: part.id,
        status: "settled",
        item: {
          kind: "slot",
          block: "activity",
          node: (
            <SubagentBlock
              part={spawn}
              onOpenChat={child && openSpawn ? () => openSpawn(child) : undefined}
            />
          ),
        },
      };
    }
    case "subagent": {
      const child = part.childSessionId;
      const openChild = ctx.onSubagentOpen;
      return {
        id: part.id,
        status: "settled",
        item: {
          kind: "slot",
          block: "activity",
          node: (
            <SubagentBlock
              part={subagentToolPart(part)}
              onOpenChat={child && openChild ? () => openChild(child) : undefined}
            />
          ),
        },
      };
    }
    case "question":
      if (part.questionKind === "plan_approval") {
        return {
          id: part.id,
          status: "settled",
          item: {
            kind: "slot",
            block: "plan",
            node: (
              <PlanBlock
                content={part.planMarkdown ?? ""}
                resolution={planResolutionOf(part)}
                planMode={MODE_CHOICES.plan}
                onOpen={() => ctx.onPlanOpen(part)}
              />
            ),
          },
        };
      }
      if (part.questions.length === 0) return null;
      return {
        id: part.id,
        status: "settled",
        item: { kind: "slot", block: "question", node: questionSlotNode(part, ctx) },
      };
    case "compaction":
      // Never a slash row: nobody typed this, and a cloud chat refuses slash
      // commands outright, so a `/compact` token in the transcript named a
      // thing the reader could not have done and cannot do.
      return {
        id: part.id,
        status: part.streaming ? "running" : "settled",
        item: {
          kind: "slot",
          block: "compaction",
          node: (
            <CompactionCard
              running={part.streaming}
              startedAt={part.startedAt}
              summarisedTurns={part.summarisedTurns}
              tokensBefore={part.tokensBefore}
              tokensAfter={part.tokensAfter}
              summary={part.text}
              onOpen={ctx.onCompactionOpen ? () => ctx.onCompactionOpen?.(part) : undefined}
            />
          ),
        },
      };
    case "command":
      if (part.tone === "error") {
        return {
          id: part.id,
          status: "settled",
          item: { kind: "notice", level: "danger", title: part.label, body: part.detail ?? "" },
        };
      }
      return {
        id: part.id,
        status: "settled",
        item: {
          kind: "slash",
          command: `/${part.command}`,
          outcome: part.label,
          data:
            part.detail ??
            part.stats?.map((stat) => `${stat.label} ${stat.value}`).join(" · ") ??
            "",
        },
      };
    case "system": {
      if (!part.text.trim()) return null;
      const note = part.failureCause
        ? (ctx.failureNote?.(part.failureCause, {
            resetsAt: part.failureResetsAt,
            manageUrl: part.failureManageUrl,
          }) ?? null)
        : null;
      return {
        id: part.id,
        status: "settled",
        item: {
          kind: "notice",
          level: part.tone === "error" ? "danger" : part.tone === "warning" ? "warning" : "neutral",
          title: part.text,
          // A failure's own figures (a refusal's amounts) lead; the reader's
          // next step follows on the same line.
          body: part.failureCause
            ? [part.detail, note?.body].filter(Boolean).join(" ")
            : part.detail || (note?.body ?? ""),
          action: note?.action ?? null,
        },
      };
    }
    case "permission":
      return settledAskEntry(part);
    case "turn_summary":
    case "file_edited":
    case "plan":
      return null;
    default:
      return part satisfies never;
  }
}

/** A permission ask a person was shown, once settled: one quiet line naming
 *  the outcome, who decided and where — "Allowed in Slack by Sam Lee",
 *  "Decided by the new mode" — over what was asked. The words are the shared
 *  outcome (`resolutionLine`), the same line the Slack card settles to. A
 *  pending ask lives in the dock, and an ask the policy settled before anybody
 *  was asked leaves no entry: nobody saw a question to see answered. */
function settledAskEntry(part: PermissionConversationPart): TranscriptEntry | null {
  if (part.status !== "resolved" || part.asked !== true) return null;
  const title = resolutionLine(
    part.selectedOptionId,
    part.decidedBy ?? "user",
    part.decidedByName,
    part.decidedVia,
  );
  // What was decided on, not the question that asked it: once the ask is
  // settled, "Run this command?" over the outcome reads as still waiting.
  const presented = presentPermission(askFromPart(part));
  return {
    id: part.id,
    status: "settled",
    item: {
      kind: "notice",
      level: "neutral",
      title,
      body: presented.subject?.text ?? presented.label,
    },
  };
}

/** The step a part contributes to a tool run, or null when it stands alone. */
function runStep(part: ConversationPart, env: StepEnvironment): CardStep | null {
  if (part.kind === "tool") return resolveStep(part, env);
  if (part.kind === "file_edited") return fileEditedStep(part, env);
  if (part.kind === "plan") return planEntriesStep(part, env);
  return null;
}

/** What a message that was never run says about itself.
 *
 *  Only the reason the box actually gave is spelled out. A word this build does
 *  not know is shown as the bare fact — the message was not sent — rather than
 *  guessed at or printed raw, so the box can name a new reason without waiting
 *  on a reader that understands it. */
const PROMPT_NOT_SENT = "Not sent";
const CANCELLED_NOTES: Record<string, string> = {
  stopped: `${PROMPT_NOT_SENT} (stopped)`,
};

/** The line under a message the box dropped, or null while it still stands. */
function cancellationOf(turn: ConversationTurn, ctx: EntryContext): TicketCancellation | null {
  if (turn.status !== "cancelled" || turn.author !== "user") return null;
  const note = (turn.cancelledReason && CANCELLED_NOTES[turn.cancelledReason]) || PROMPT_NOT_SENT;
  const resend = ctx.onResend;
  // The words the reader wrote, as the transcript holds them — what a resend
  // puts back through the send path, not a re-run of the dead turn.
  const said = turn.parts
    .filter((part) => part.kind === "text")
    .map((part) => part.text)
    .join("\n")
    .trim();
  if (!resend || !said) return { note };
  return { note, onResend: () => resend(said) };
}

/** User prose always stands alone and terminates any preceding tool run. */
function userEntries(turn: ConversationTurn, ctx: EntryContext): TranscriptEntry[] {
  // A message the server has taken but no box has picked up yet reads as still
  // going out, in place of the time it was said: the words are durable, the
  // turn is not under way.
  const sending = turn.status === "running";
  const cancelled = cancellationOf(turn, ctx);
  const texts = turn.parts.filter((part) => part.kind === "text");
  return texts.map((part, index) => ({
    id: part.id,
    status: sending ? ("running" as const) : ("settled" as const),
    item: {
      kind: "user" as const,
      text: part.text,
      at: timeLabel(part.time ?? turn.startedAt),
      // Where the message came from is said once, at its top, not again under
      // every block it was written in.
      ...(turn.fromTemplate && index === 0
        ? {
            fromTemplate: turn.fromTemplate,
            ...(turn.fromTemplateAuthor ? { fromTemplateAuthor: turn.fromTemplateAuthor } : {}),
          }
        : {}),
      ...(sending ? { pending: true as const } : {}),
      // The message was dropped once, not once per block it was written in:
      // the line and its control ride the last of them.
      ...(cancelled && index === texts.length - 1 ? { cancelled } : {}),
    },
  }));
}

/** Renumber whatever repeats, keeping the first of each id as it was.
 *
 *  React's own warning for a repeated key ends "may cause children to be
 *  duplicated and/or omitted" — on a transcript that is a row of the
 *  conversation silently vanishing. Ids are unique in a well-formed fold, but a
 *  fold is built from whatever a publisher sent, so the guarantee is made HERE
 *  rather than assumed. */
function renumbered<T extends { id: string }>(items: T[]): T[] {
  const seen = new Map<string, number>();
  return items.map((item) => {
    const before = seen.get(item.id) ?? 0;
    seen.set(item.id, before + 1);
    return before === 0 ? item : { ...item, id: `${item.id}#${before}` };
  });
}

/** What a dimmed run of turns says about itself, once, at its top. */
const CLEARED_NOTE: Record<"cleared" | "summarised", string> = {
  cleared: "cleared",
  summarised: "summarised",
};

/** The tape keys on the entry id. */
function withUniqueIds(entries: TranscriptEntry[]): TranscriptEntry[] {
  return renumbered(entries);
}

/** Whether an ask in this turn is waiting on a person, which holds the turn's
 *  unanswered calls: nothing it gates runs until someone answers. */
function holdsCalls(turn: ConversationTurn): boolean {
  return turn.parts.some(
    (part) => part.kind === "permission" && part.status === "pending" && part.prompting === true,
  );
}

/** The turns whose calls an unanswered ask holds. Read off the turns before the
 *  open asks are stripped from the tape, since the ask itself never renders. */
export function heldTurnIds(turns: ConversationTurn[]): ReadonlySet<string> {
  return new Set(turns.filter(holdsCalls).map((turn) => turn.id));
}

export function transcriptEntries(turns: ConversationTurn[], ctx: EntryContext): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [];
  const env: StepEnvironment = {
    onOpenUrl: ctx.onOpenUrl,
    onOpenPath: ctx.onLinkClick,
    workspaceRoot: ctx.workspaceRoot,
    onOpenLineageNode: ctx.onLineageOpen,
    onOpenKnowledgeItem: ctx.onKnowledgeOpen,
    notebookImage: ctx.notebookImage,
    notebookChartSpec: ctx.notebookChartSpec,
    onOpenNotebookCell: ctx.onOpenNotebookCell,
  };

  let buffer: ActivityBuffer | null = null;
  const flush = (spoken = false) => {
    if (!buffer) return;
    const done = !buffer.running && (spoken || buffer.turnDone);
    entries.push({
      id: buffer.id,
      status: done ? "settled" : "running",
      item: {
        kind: "activity",
        summary: `Ran ${buffer.steps.length} tools`,
        // A step is keyed by the tool call it came from, inside the card, so
        // the same guarantee has to hold one level down: an activity holding
        // two calls a publisher gave the same id (or no id at all) is exactly
        // where the reader loses a query off the card.
        steps: renumbered(buffer.steps),
      },
    });
    buffer = null;
  };

  // A run of turns the agent no longer holds is dimmed as one, and says why
  // ONCE — at the top of the run, not on every line inside it.
  let dimNoted = false;
  const markDim = (turn: ConversationTurn, from: number): void => {
    if (!turn.cleared) {
      dimNoted = false;
      return;
    }
    for (let i = from; i < entries.length; i += 1) {
      const entry = entries[i];
      if (!entry) continue;
      entry.dimmed = true;
      if (!dimNoted) {
        entry.dimNote = CLEARED_NOTE[turn.clearedReason ?? "cleared"];
        dimNoted = true;
      }
    }
  };

  for (const turn of turns) {
    if (turn.author === "user") {
      flush(true);
      const from = entries.length;
      entries.push(...userEntries(turn, ctx));
      markDim(turn, from);
      continue;
    }

    const from = entries.length;
    // A pending ask that waits on a person holds this turn's unanswered calls.
    const held = ctx.heldTurnIds?.has(turn.id) === true || holdsCalls(turn);
    const turnEnv = held ? { ...env, awaitingApproval: true } : env;
    for (const [index, part] of turn.parts.entries()) {
      const step = runStep(part, turnEnv);
      if (step) {
        const running = step.status === "running" || step.status === "pending";
        if (buffer) {
          buffer.steps.push(step);
          buffer.running ||= running;
          buffer.turnDone = Boolean(turn.completedAt);
        } else {
          buffer = {
            id: part.id,
            steps: [step],
            running,
            turnDone: Boolean(turn.completedAt),
          };
        }
        continue;
      }
      const entry = partEntry(part, ctx, turn, index);
      if (entry) {
        flush(part.kind === "text");
        entries.push(entry);
      }
    }
    markDim(turn, from);
  }
  flush();
  return withUniqueIds(entries);
}

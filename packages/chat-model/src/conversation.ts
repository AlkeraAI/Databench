// The canonical conversation model — the turn/part shapes the harness event fold produces
// and every chat surface consumes. React-free by design: UI libraries depend on this
// package, never the reverse. Canonical field names follow the fold (the wire truth);
// where a UI library historically drifted, this file wins and the consumer adjusts.

export type WorktreeState = "working" | "ready" | "merging" | "merged";

export type ConversationRole = "user" | "assistant" | "tool" | "system";

export type ResourceKind =
  | "file"
  | "diff"
  | "data_source"
  | "sql"
  | "graph"
  | "run"
  | "node"
  | "artifact"
  | "external";

export interface ResourcePreview {
  kind: "text" | "diff" | "table" | "json" | "terminal";
  title?: string;
  content?: string;
  columns?: string[];
  rows?: Array<Record<string, string | number | boolean | null>>;
  truncated?: boolean;
}

/** ui's historical name for `ResourcePreview` — one shape, two names during the
 *  migration; the alias retires when its importers converge on the canonical name. */
export type ResourcePreviewData = ResourcePreview;

export interface ResourceReference {
  kind: ResourceKind;
  label: string;
  target: string;
  path?: string;
  line?: number;
  range?: { startLine: number; endLine: number };
  diff?: { insertions: number; deletions: number };
  preview?: ResourcePreview;
}

export type ConversationAuthor =
  | "user"
  | "assistant"
  | "coordinator"
  | "subagent"
  | "system";

export type ConversationPart =
  | TextConversationPart
  | ThinkingConversationPart
  | ToolConversationPart
  | PermissionConversationPart
  | QuestionConversationPart
  | PlanConversationPart
  | FileEditedConversationPart
  | SubagentConversationPart
  | CompactionConversationPart
  | SystemConversationPart
  | CommandConversationPart
  | TurnSummaryConversationPart;

export interface ConversationTurn {
  id: string;
  author: ConversationAuthor;
  agentLabel?: string;
  title?: string;
  startedAt?: string;
  completedAt?: string;
  status?: "running" | "done" | "error" | "cancelled";
  /** Why a cancelled turn was cancelled, in the word the server stamped on it.
   *  A reader never invents one: a turn cancelled with no word carries none,
   *  and a word this reader does not recognise is shown as no word rather than
   *  guessed at — so a later reason needs no coordinated release. */
  cancelledReason?: string;
  /** History the agent no longer has in its live context — still shown, but
   *  dimmed. Set by a /clear (the whole prior scrollback) or by a compaction
   *  (the turns it elided behind the summary), automatic or manual alike. */
  cleared?: boolean;
  /** Why the turn left the agent's context, where the transcript says so on
   *  the turn itself. A compaction's turns are "summarised" — the summary that
   *  replaced them is in the chat; a /clear's are not. */
  clearedReason?: "cleared" | "summarised";
  /** Conversation tokens the model carried when this turn finished — input
   *  plus both cache halves, the figure a context window is spent against.
   *  Absent on a turn whose usage the harness did not report. */
  contextTokens?: number;
  /** The template whose brief opened this chat, when the first message is one
   *  the server sent rather than one the reader typed. Its words are still
   *  theirs to edit and re-send; this only says where they came from. */
  fromTemplate?: string;
  /** Who wrote that template's brief. On a shared template these are another
   *  person's words opening this reader's chat, which is the whole reason the
   *  bubble names them. Absent when the author could not be resolved. */
  fromTemplateAuthor?: string;
  parts: ConversationPart[];
}

export interface ConversationPartBase {
  id: string;
  time?: string;
  streaming?: boolean;
}

export interface TextConversationPart extends ConversationPartBase {
  kind: "text";
  text: string;
  resources?: ResourceReference[];
}

export interface ThinkingConversationPart extends ConversationPartBase {
  kind: "thinking";
  text: string;
}

/** Where a tool call stands. `stopped` is a call that ended WITHOUT a result
 *  because its turn was stopped while it ran: not a failure of the tool (that is
 *  `error`), and not a call that answered with nothing (that is `completed` with
 *  an empty output). */
export type ToolState = "pending" | "running" | "completed" | "error" | "stopped";

/** Whether a call has ended, however it ended. */
export function toolSettled(state: ToolState): boolean {
  return state === "completed" || state === "error" || state === "stopped";
}

/** A handle to a tool-produced blob (a big table, long text/JSON, a
 *  screenshot) surfaced in the transcript as a named chip that opens the full,
 *  paginated content read-only in the editor page. `name` is the friendly,
 *  succinct label the producing tool gave the result; `refType` is an optional
 *  declared renderer key (else discovered from the fetched page's kind). */
export interface BlobReference {
  handle: string;
  name: string;
  refType?: string;
  mime?: string;
}

export interface ToolConversationPart extends ConversationPartBase {
  kind: "tool";
  callId: string;
  name: string;
  toolKind?: string;
  state: ToolState;
  /** A backgrounded SQL/bash job's lifecycle card — runs detached, notifies on
   *  finish. The card renders a "Background" label so it reads as not-inline. */
  background?: boolean;
  input?: Record<string, unknown>;
  output?: Record<string, unknown> | string | null;
  errorText?: string | null;
  content?: string;
  resources?: ResourceReference[];
  /** Blob results the tool produced — rendered as inline reference chips. */
  references?: BlobReference[];
  /** Bytes the publisher dropped to fit this result into one transcript row.
   *  Set only when it cut something, so a card can say how much is not on
   *  screen instead of leaving a silently shortened result looking whole. */
  truncatedBytes?: number;
  /** For a `spawn_agent` tool: the spawned child chat's session id. Bound EARLY
   *  from the `SubagentStarted` pointer (matched by agent + prompt, since the MCP
   *  transport drops the tool-call id) so the card can stream the running child's
   *  tool calls and drill into its live transcript before the blocking call
   *  returns. Also present on the final result of a completed spawn. */
  childSessionId?: string;
  /** For a running `spawn_agent` tool: a compact, live snapshot of the child's
   *  recent tool calls, mirrored from the observed child stream by the data layer.
   *  Ignored once the tool completes (the card collapses to the report). */
  childActivity?: SubagentActivity;
}

/** A compact, live view of a running subagent's work — the child's most-recent
 *  tool calls plus a running total — mirrored onto its `spawn_agent` tool part so
 *  the (pure) card can render live activity without reaching across chats. */
export interface SubagentActivity {
  /** The child's most-recent tool calls, oldest→newest, capped for compactness. */
  recentTools: SubagentActivityTool[];
  /** Total tool calls observed so far (may exceed `recentTools.length`). */
  toolCount: number;
  /** False once the child's turn has finished. */
  running: boolean;
  /** The child's latest assistant text, mirrored live so the spawn card's preview
   *  can stream it; the card settles to the spawn's summary once the turn ends. */
  lastMessage?: string;
  /** Per-tool call counts so far, keyed by tool name — the live source for the
   *  spawn card's tool-call pills (the running mirror of the result's `by_tool`). */
  byTool?: Record<string, number>;
  /** The model + effort the child runs with, mirrored so the live footer leads
   *  with them exactly as the settled footer does. */
  model?: string;
  effort?: string;
  /** Token usage accrued so far. */
  tokens?: { input?: number; output?: number };
}

export interface SubagentActivityTool {
  callId: string;
  name: string;
  state: ToolState;
  /** A short target hint (file path / command / query), when the input carries one. */
  detail?: string;
}

export type PermissionOptionId =
  | "allow_once"
  | "allow_always"
  | "reject_once"
  | "reject_always"
  | "cancelled";

export interface PermissionOptionView {
  optionId: PermissionOptionId | string;
  name: string;
}

/** The blast radius of a gated action: read observes, write recoverably
 *  mutates, destroy is irreversible/wide, egress moves data OUT, exec runs a
 *  program or reaches the filesystem on the data server itself, memory records
 *  what the agent learned. Drives the loud (red) treatment for
 *  destroy/egress/exec on the permission card.
 *
 *  `unknown` is a tier this build has no slot for — a classifier ahead of it,
 *  or a word nobody here has seen. It is a MEMBER rather than an absence
 *  because "the classifier said something we cannot read" and "the classifier
 *  said nothing" are different facts, and the surfaces that gate on the effect
 *  answer them differently: an unreadable verdict is not a read, while no
 *  verdict at all leaves the ask's kind to speak for it. Collapsing the two
 *  offered an approval the machine then dropped. */
export type PermissionEffect =
  | "read"
  | "write"
  | "destroy"
  | "egress"
  | "exec"
  | "memory"
  | "unknown";

/** One resource a gated action touches (a table, file, schema, …). Every target
 *  is treated equally — we deliberately don't infer or surface a prod/dev
 *  environment (that distinction is unreliable; stay cautious about all of them). */
export interface PermissionTargetView {
  kind: string;
  name: string;
  connection?: string;
}

/** The estimated cost of a gated warehouse action, when the classifier could
 *  derive one (e.g. a SQL `bytes_scanned` price). */
export interface PermissionCostView {
  usd?: number;
  bytesScanned?: number;
  rowsScanned?: number;
  currency?: string;
}

/** The serialized ActionDescriptor (`subject`) that a permission request gates
 *  — capability + effect tier + targets + cost + classifier confidence. Folded
 *  from `permission.request.subject`; absent for adapters that don't classify. */
export interface PermissionSubjectView {
  capability?: string;
  effect?: PermissionEffect;
  operation?: string;
  targets: PermissionTargetView[];
  cost?: PermissionCostView;
  confidence?: "exact" | "heuristic" | "unknown";
  reasons: string[];
  /** What a standing grant records: the classified operation's whole family, or
   *  (`command`) this exact text, for a compound command that has no family. */
  scope?: "operation" | "command";
}

/** One asset downstream of the gated change, with the edge taxonomy that carried
 *  the change to it. A `transformation` of "unknown" is shown, never hidden,
 *  because such an edge is why a destructive change escalates. The
 *  classifier's own category words ride through verbatim, so an unrecognized one
 *  renders as itself rather than being rounded down to safe. */
export interface PermissionAffectedView {
  urn: string;
  category: string;
  transformation: string;
  reason?: string;
}

/** A knowledge item filed against an affected asset. Its meaning is what the
 *  approver is meant to weigh before deciding. */
export interface PermissionConceptView {
  urn: string;
  itemId: string;
  title: string;
  meaning: string;
  ownerTeams: string[];
}

/** What the lineage graph says a gated write reaches, folded from
 *  `permission.request.impact` (schema 1.4.0). A `degraded` status means part of
 *  the read gave out, so a short affected list is ignorance rather than safety.
 *  `reason` is the sentence that says which. Absent when no graph was consulted. */
export interface PermissionImpactView {
  status: "resolved" | "degraded";
  category: string;
  /** What the statement changes, at column grain when it named a column. */
  targets: string[];
  affected: PermissionAffectedView[];
  concepts: PermissionConceptView[];
  ownerTeams: string[];
  /** The policy could not rule alone: an ambiguous target, an unknown-severity
   *  edge under a destructive change, or two owning teams. */
  unsure: boolean;
  reason: string;
}

/** A notebook ask as the machine holding the notebook sent it
 *  (`permission.request.preview.notebook`). Cells are named for a person,
 *  never by their internal id. */
export interface PermissionNotebookView {
  lead: string;
  fileName: string;
  filePath: string;
  tail: string;
  cells: { name: string; code: string; role: "target" | "dependency" | "dependent" }[];
  packages: string[];
}

export interface PermissionConversationPart extends ConversationPartBase {
  kind: "permission";
  requestId: string;
  permissionKind: string;
  canonicalKind: "edit" | "shell" | "network" | "task" | "external" | "other";
  patterns: string[];
  options: PermissionOptionView[];
  status: "pending" | "resolved";
  /** True only when a HUMAN answer is being awaited (the broker prompted the
   *  editor). A pending request without it is still with the policy/safety
   *  judge — e.g. auto mode — and must NOT raise the interactive queue. */
  prompting?: boolean;
  /** True while the ask reached the transcript before the tool part naming
   *  what it gates; the harness raises it again, under the same id, with the
   *  subject. A card must not be answerable while this holds. */
  subjectPending?: boolean;
  /** The ids the ask gave for the tool call it gates — the harness's own and
   *  the provider's can differ — so a surface can show that call's input. */
  callIds?: string[];
  /** The proposed file change captured on the ask, before approval. */
  preview?: ResourcePreview;
  /** A notebook tool's ask, in the notebook's own words: the action, the file,
   *  and the cells it would run by the names a person knows them by. */
  notebook?: PermissionNotebookView;
  /** The typed ActionDescriptor the request gates (effect/targets/cost), when
   *  the adapter classified the action. */
  subject?: PermissionSubjectView;
  /** What the lineage graph says this write reaches, when a graph was consulted. */
  impact?: PermissionImpactView;
  selectedOptionId?: string;
  /** The member who decided, by display name, when the answer was recorded
   *  somewhere that knows the roster. A chat several people read gets several
   *  people who could have answered. Absent for a decision the harness settled
   *  on the machine, and for a policy or a timeout, which are nobody's. */
  decidedByName?: string;
  /** Who settled it: a person (`user`), the policy under a mode switched
   *  while it waited (`policy`), or a `timeout`. */
  decidedBy?: string;
  /** Where the person was when they decided (`web` / `slack`), when the
   *  server recorded it. */
  decidedVia?: string;
  /** A person was asked (the ask reached `prompting`), so its outcome is worth
   *  a line in the transcript; an ask the policy settled unseen is not. */
  asked?: boolean;
}

export interface QuestionOptionView {
  label: string;
  description?: string | null;
}

export interface QuestionPromptView {
  question: string;
  header?: string | null;
  options: QuestionOptionView[];
  multiple: boolean;
  custom: boolean;
}

export interface QuestionConversationPart extends ConversationPartBase {
  kind: "question";
  requestId: string;
  questionKind: "question" | "plan_approval";
  toolCallId?: string | null;
  questions: QuestionPromptView[];
  status: "pending" | "answered" | "rejected";
  answers?: string[][];
  reason?: string | null;
  /** What the answerer wrote for the model beside the answers. */
  note?: string | null;
  /** For a plan_approval: the plan's Markdown, read from the model's plan file by
   *  the adapter and surfaced here — so the webview renders the full plan without it
   *  being re-fed into the model's context (file-based plan mode). */
  planMarkdown?: string;
}

export interface PlanEntryView {
  id: string;
  text: string;
  status: "pending" | "in_progress" | "completed";
}

export interface PlanConversationPart extends ConversationPartBase {
  kind: "plan";
  entries: PlanEntryView[];
}

export interface FileEditedConversationPart extends ConversationPartBase {
  kind: "file_edited";
  resource: ResourceReference;
}

export interface SubagentConversationPart extends ConversationPartBase {
  kind: "subagent";
  childSessionId: string;
  name: string;
  /** The agent type (e.g. "explore", "review"), shown as a pill in the card header. */
  agent?: string;
  displayName?: string;
  avatarSeed?: string;
  instructions?: string | null;
  description?: string | null;
  status: "running" | "completed" | "error";
  summary?: string | null;
  error?: string | null;
}

export interface CompactionConversationPart extends ConversationPartBase {
  kind: "compaction";
  text: string;
  title?: string;
  /** When the fold began, so a reader watching it can see how long it has
   *  been running. Set from the `compacting` status the harness emits before
   *  the summary exists; absent where only the settled event was seen. */
  startedAt?: string;
  /** How many turns the summary replaced. Zero is a real answer (the harness
   *  folded nothing but the window still shrank), so the card states the
   *  turn count only when there is one. */
  summarisedTurns?: number;
  /** Conversation tokens held before and after the fold, so the card can say
   *  what the compaction bought. Absent until the numbers are known — the
   *  `after` figure only exists once the next reply reports its usage. */
  tokensBefore?: number;
  tokensAfter?: number;
}

export interface SystemConversationPart extends ConversationPartBase {
  kind: "system";
  text: string;
  /** A second, quieter line under the notice — the statement a refusal quotes.
   *  Plain text: the renderer never treats it as markup. */
  detail?: string;
  tone?: "info" | "warning" | "error";
  /** Why the turn failed, where this notice reports a failure — the tag a
   *  shell maps to the one next step that cause has (a plan page, a Retry).
   *  Absent on a notice that is not a failure. Open by design: an unknown tag
   *  renders as a plain notice rather than breaking the reader's transcript. */
  failureCause?: string;
  /** Where the failure is a money refusal: when the spent cycle resets, and
   *  the plan page when the wire decided this reader may open it. Both are
   *  facts the shell's next step is built from, never shown raw. */
  failureResetsAt?: string;
  failureManageUrl?: string;
}

/** A slash command's outcome, rendered as a native card (not a text dump):
 *  the daemon returns structured payloads (chat_slash.dispatch_ui) and the
 *  webview maps them onto this shape. */
export interface CommandConversationPart extends ConversationPartBase {
  kind: "command";
  /** Canonical command name (compact / clear / title / usage / …). */
  command: string;
  label: string;
  detail?: string;
  /** Small stat grid (e.g. /usage totals). */
  stats?: { label: string; value: string }[];
  tone?: "info" | "error";
}

export interface TurnSummaryConversationPart extends ConversationPartBase {
  kind: "turn_summary";
  summary?: string;
  stopReason?: string;
  costUsd?: number | null;
  tokens?: Record<string, number> | null;
  files: ResourceReference[];
  importantFiles?: ResourceReference[];
  artifacts?: ResourceReference[];
  graphNodes?: ResourceReference[];
  runs?: ResourceReference[];
  errorDetail?: string | null;
}

export interface ConversationMessage {
  id: string;
  role: ConversationRole;
  content: string;
  toolName?: string;
  /** Optional agent identifier (e.g. "coordinator", "agent_draft_sql").
   *  When set, the chat surface renders a colored chip prefix derived
   *  deterministically from the label. */
  agentLabel?: string;
  /** Render variant that refines the base role. `thinking` shows the message
   *  as a collapsible reasoning block (the data role collapses to assistant). */
  variant?: "thinking";
  /** True while this message is still streaming in — the surface shows a live
   *  caret and keeps the block expanded. */
  streaming?: boolean;
}

export interface SidecarWorktree {
  id: string;
  branch: string;
  state: WorktreeState;
  /** Optional human-readable label; falls back to `branch`. */
  label?: string;
  /** Diff line counts (insertions / deletions). Rendered when both are set. */
  diff?: { insertions: number; deletions: number };
}

export interface SidecarGraph {
  id: string;
  name: string;
  description?: string;
  nodeCount?: number;
  worktrees: SidecarWorktree[];
}

export interface SidecarChat {
  id: string;
  title: string;
  /** ISO timestamp for compact chat-list recency labels. */
  updatedAt?: string;
  /** The agent is actively working this chat (a turn awaits its response) —
   *  renders a pulse dot so a backgrounded chat's activity stays visible. */
  active?: boolean;
  /** Background jobs (bash / sql / subagent) running in this chat right now —
   *  renders a `⟳N` badge so a chat reads as busy even when its foreground turn
   *  is idle (orthogonal to `active`). Absent / 0 → no badge. */
  backgroundJobsRunning?: number;
  graphs: SidecarGraph[];
}

export interface SidecarChatMenuAction {
  id: "openInEditor" | "rename" | "delete";
  label: string;
  destructive?: boolean;
}

export interface ComposerCommand {
  id: string;
  /** The leading slash trigger token (without the slash). e.g. `"new"`. */
  trigger: string;
  /** Visible label (typically `/new`). */
  label: string;
  description?: string;
  /** Argument shape, the CLI's convention: `()` required, `[]` optional.
   *  Shown in the menu once the trigger is fully typed. */
  usage?: string;
  disabled?: boolean;
  /** How selecting the command behaves. `panel` swaps the list for an inline
   *  panel (the host's `renderCommandPanel`); `immediate`/`surface` run the
   *  host's `onCommandRun`. Omitted → the default `onCommand`/pre-fill path. */
  present?: "panel" | "immediate" | "surface";
  /** Keep the command out of the browsable menu while still resolving a typed
   *  invocation (e.g. the `/plan` mode shortcut behind the `/mode` panel). */
  hidden?: boolean;
}

export interface ComposerFileResult {
  id: string;
  label: string;
  path: string;
}

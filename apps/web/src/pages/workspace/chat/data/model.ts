// The chat surface's own data vocabulary — the shapes a chat data source hands
// the host composition, independent of which source produced them (the VS Code
// daemon or the cloud REST + doc-sync pair).
//
// These types moved here out of the webview's data barrel when the chat host was
// ported into the browser tree: the browser portal may not import the webview, so
// the model has to live on the side both shells can reach. The webview's barrel
// re-exports every name, which is why nothing that imported them had to change.

import type { ConversationTurn } from "@alkera/chat-model";

export type ChatEventKind =
  | "assistant_text"
  | "tool_call"
  | "tool_result"
  | "graph_changed"
  /** Older turns joined the window above what was on screen (a page scrolled
   *  into, or pages released). The store re-reads the turns without counting
   *  the new user turns as echoes of anything it sent. */
  | "history_loaded"
  | "thinking";

/** The chat's pinned model (from the manifest's chat metadata) — set when the
 *  harness instantiates on the first message and immutable afterwards. */
export interface PinnedModel {
  id: string | null;
  efforts: string[];
  /** The persisted reasoning-effort choice (manifest.model.effort). */
  effort: string | null;
  /** How many tokens this model's window holds, as the catalog said when the
   *  chat pinned it. The composer's context readout spends against it, so a
   *  reader can see a compaction coming. Absent (or 0) where the source does
   *  not record it — the readout then states the count and draws no share. */
  contextWindow?: number;
}

export interface Chat {
  id: string;
  title: string;
  updatedAt: string;
  /** Present once the chat exists daemon-side (model pinned at creation). */
  model?: PinnedModel;
  /** The chat's persisted permission mode — the synchronous seed for the
   *  composer pill on a chat switch (so it doesn't strand the previous chat's
   *  mode while the daemon read is in flight). */
  permissionMode?: PermissionMode;
  /** Set on subagent (child) chats — the session that spawned this one. Root
   *  chats have `null`; the sidebar lists root chats only (the CLI does too). */
  parentSessionId?: string | null;
  /** How many background jobs (bash / sql / subagent) are running RIGHT NOW in
   *  this chat. Drives the "active in the background" badge in the chat list, so
   *  a chat reads as busy even when its foreground turn is idle. Absent / `0` for
   *  a chat that isn't open daemon-side and on every fresh load (jobs don't
   *  survive a daemon restart); read it as `?? 0`. */
  backgroundJobsRunning?: number;
  /** The daemon's live view of the chat's turn: "running" while a foreground
   *  turn is active (in this daemon, or a live lock holder elsewhere). Derived,
   *  never persisted — a crashed process can't leave a stale spinner. Fills the
   *  home row's mark for chats this window has no fold for. */
  status?: "idle" | "running";
  /** What the chat is waiting on the human for, per the daemon's live brokers.
   *  Null while it owes nothing, and always null for a chat no live process
   *  has open. */
  pendingAsk?: ChatAsk | null;
  /** The persisted tail of the chat's event log (the manifest cache). */
  lastEventId?: string | null;
  /** The tail the user's client had rendered when it last marked the chat
   *  seen. Differing from `lastEventId` (or never stamped) = unread. */
  lastSeenEventId?: string | null;
}

/** What a chat has stopped to ask the human for. */
export type ChatAsk = "permission" | "question" | "plan";

/** A chat's live state this session, folded from the events the daemon streamed
 *  whether or not the chat's route was ever mounted. A chat with no folded state
 *  simply has no entry. */
export interface ChatActivity {
  /** The agent owes a response, which includes waiting on an ask. */
  awaiting: boolean;
  /** The last turn boundary (a send or a turn end), for the home list's sort. */
  lastInteractionAt: string | null;
  /** The last event of any kind, so a row can age on tool work too. */
  lastEventAt?: string;
  /** What the chat is waiting on the human for, while it is. */
  ask?: ChatAsk;
  /** When the chat last finished a turn or raised an ask. These are the only two
   *  events that move a chat to the top of the home list; everything else
   *  refreshes a row where it stands. */
  bumpAt?: string;
}

export interface ChatEvent {
  id: string;
  chatId: string;
  kind: ChatEventKind;
  content: string;
  toolName?: string;
  graphId?: string;
  /** Identifies which agent emitted this message — e.g. "coordinator",
   *  "agent_draft_sql". The UI renders this as a colored chip prefix so
   *  the user can see which sub-agent is talking / calling tools. */
  agentLabel?: string;
  /** True when this update is the open-chat REPLAY (initial history load), not
   *  live activity. The chat store reconciles a replay as non-live so reopening
   *  a chat whose last turn never got its (late) completion event doesn't light
   *  the working indicator. */
  replay?: boolean;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "tool" | "system" | "thinking";
  content: string;
  toolName?: string;
  /** Agent identifier; see ChatEvent.agentLabel. */
  agentLabel?: string;
}

export type ChatTurn = ConversationTurn;

export interface FileSearchResult {
  id: string;
  label: string;
  path: string;
}

/** A model the OpenCode gateway can serve, surfaced in the composer's model
 *  picker. Mirrors the daemon's `GatewayModelInfo` (camelCased). */
export interface ModelInfo {
  id: string;
  displayName: string;
  wire: "anthropic" | "openai";
  efforts: string[];
  defaultEffort: string | null;
}

/** One model in an open chat's picker, with the server's verdict on moving the
 *  chat to it. `message` is the sentence every surface shows for an
 *  unavailable model; `escapeNewChatModel` names the model a new chat would
 *  use when that is the way out. */
export interface ModelOption {
  model: ModelInfo;
  state: "current" | "available" | "unavailable";
  reasonCode: string | null;
  message: string | null;
  /** The reason without the model's name: one line over every model it covers. */
  groupMessage?: string | null;
  escapeNewChatModel: string | null;
}

/** What an open chat's model picker offers this reader. `applies` says when a
 *  switch takes effect: the next turn, or once the chat's agent restarts. */
export interface ModelOptions {
  currentModelId: string | null;
  currentEffort: string | null;
  canSwitch: boolean;
  applies: "next_turn" | "after_reopen";
  options: ModelOption[];
  /** The options are the chat owner's plan (they pay for its turns), and this
   *  reader is not the owner. */
  billedToOwner?: boolean;
}

/** The new-chat seed (Default Chat Model + Effort), resolved daemon-side against
 *  the live catalog from the user's saved preferences. `null` = nothing to seed
 *  (the composer falls back to the first catalog model / its default effort).
 *
 *  `permissionMode` is the stance a chat started NOW would open in, answered by
 *  whoever actually decides it (the server, for a cloud chat). `null` = this
 *  source does not decide it up front, and the composer falls back. */
export interface ChatDefaults {
  model: string | null;
  effort: string | null;
  permissionMode: string | null;
}

/** One slash command from the daemon's registry (the CLI's own table —
 *  Python is the single source of truth; the UI only filters and renders). */
export interface SlashCommandInfo {
  name: string;
  aliases: string[];
  summary: string;
  /** `()` marks a required arg, `[]` an optional one (the CLI convention). */
  usage: string;
  /** Dispatchable but left out of menus (the mode shortcuts). */
  hidden: boolean;
  /** False for CLI-only commands (the editor has a native equivalent). */
  ui: boolean;
}

/** Structured outcome of a slash command (chat_slash.dispatch_ui daemon-side):
 *  editors draw native UI from `payload`; `cli_only` answers a REPL-scoped
 *  command with a hint at the editor's equivalent. */
export interface SlashCommandOutcome {
  kind: "ok" | "bad_usage" | "exit" | "cli_only" | "unknown";
  command: string | null;
  payload: Record<string, unknown>;
  message: string | null;
}

/** Permission stance for a chat — parity with the CLI's `/mode`. Mirrors the
 *  daemon's `PermissionMode` literal. */
export type PermissionMode = "read_only" | "default" | "auto" | "plan" | "bypass";

/** Engine options chosen in the composer. The model is pinned when the chat is
 *  first created; effort + mode apply per turn. Data sources that don't route
 *  through the OpenCode engine ignore these. */
export interface SendOptions {
  model?: ModelInfo;
  effort?: string;
  mode?: PermissionMode;
  /** Files node ids already linked to the chat, sent with this message. A
   *  source with no Files behind it ignores them. */
  attachments?: string[];
  /** The id the server dedupes this message by. One per message, kept across
   *  every retry of it, so a send that reached the server before its answer
   *  was lost is recorded once. A source that mints its own ignores it. */
  clientId?: string;
  /** The workspace a NEW chat is started in. Read only by a create; a source
   *  with no workspaces ignores it. */
  workspaceId?: string;
}

export type Unsubscribe = () => void;


// The contract the chat host composition is written against.
//
// One chat frontend, two shells. In VS Code the transcript comes from the local
// daemon over the extension's JSON-RPC bridge; in the browser it comes from the
// cloud's REST surface plus the doc-sync socket. Everything above this file —
// the surfaces, the controller hooks, the transcript translation, every tool
// card — is identical in both, because both shells hand the same composition an
// object of this shape.
//
// The interface is deliberately NARROW: it is exactly what the chat host calls,
// not everything a data source can do. A source is free to be larger (the
// daemon's is, by an order of magnitude); it only has to satisfy this.
//
// Return types are the narrowest the host actually reads, so a source can return
// its own richer record without an adapter (`listContext` really returns a page
// of items — the chat header only ever reads the count off it).

import type {
  BlobPage,
  CostLedgerEntryView,
  CostStateView,
  DecisionView,
  PermissionConversationPart,
  SuggestedPrompt,
} from "@alkera/chat-model";
import { PERMISSION_MODE_VALUES } from "@alkera/chat-model";

import type { LiveDraft } from "../../../../api/realtime/crdt/liveDraft";
import type { ChatFilesPort } from "./chatFiles";
import type {
  Chat,
  ChatActivity,
  ChatDefaults,
  ChatEvent,
  ChatTurn,
  ChatMessage,
  FileSearchResult,
  ModelInfo,
  ModelOptions,
  PermissionMode,
  SendOptions,
  SlashCommandInfo,
  SlashCommandOutcome,
  Unsubscribe,
} from "./model";

/** Whether approving an ask would reach the agent — and, where it would not,
 *  the sentence the card says in place of the approving keys.
 *
 *  One answer rather than a boolean beside a lookup for the words: the stance
 *  that withholds the Allow is the same stance that explains it, and two
 *  surfaces keyed on the same fact are two that can disagree about it. A card
 *  that withheld the Allow while saying the workspace was read-only, in a chat
 *  the reader had just put in another stance, is what that disagreement costs. */
export type ApprovalVerdict = { allowed: true } | { allowed: false; refusal: string };

/** The verdict for an ask the machine would act on. */
export const APPROVAL_REACHES: ApprovalVerdict = { allowed: true };

/** The floor's own sentence: a workspace that runs nothing says so. Spelled
 *  once, because the cloud's stance table names it for `read_only` and the
 *  verdict below is the same refusal reached without a chat to read a stance
 *  from. */
export const READ_ONLY_REFUSAL = "This workspace is read-only, so it won't run this.";

/** The verdict while the server's word on a chat's stance has not arrived:
 *  no Allow yet, and a sentence that names no stance the chat may not be in. */
export const APPROVAL_PENDING: ApprovalVerdict = { allowed: false, refusal: "Checking this chat's mode…" };

/** The verdict where there is no chat to decide against. An unknown is read as
 *  the most restrictive stance, never the least. */
export const APPROVAL_WITHHELD: ApprovalVerdict = {
  allowed: false,
  refusal: READ_ONLY_REFUSAL,
};

/** What a shell's chat source can actually do, so a surface gates on a fact
 *  rather than on a hard-coded constant. `opencodeActive` was
 *  `webview/data/index.ts`'s `isOpencodeActive = true`: the engine-shaped
 *  affordances (the model picker, the effort pill, the per-chat mode read) only
 *  make sense against a source that drives an OpenCode harness. */
export interface ChatCapabilities {
  /** The source drives an OpenCode harness, so engine-shaped reads (models,
   *  effort, permission mode) are worth issuing. */
  readonly opencodeActive: boolean;
  /** The permission mode EVERY session on this source runs under, where the
   *  source fixes it rather than letting the reader choose.
   *
   *  A chat reads its own mode through `getPermissionMode`, but the chat HOME
   *  has no chat to ask about — and showing "Default" there over a source that
   *  opens every session read-only is the first control a reader meets stating
   *  a mode their session will not have. Undefined where the mode is per-chat. */
  readonly fixedPermissionMode?: string;
  /** The source serves a model catalogue, so `listModels` / `resolveChatDefaults`
   *  are worth issuing and the composer draws a real picker.
   *
   *  Held apart from `opencodeActive` because they stopped being the same fact:
   *  the browser has a catalogue (the gateway's, over the portal's own route)
   *  and still drives no OpenCode harness, so it has no slash-command registry
   *  and no effort to set on a running session. Unset = follow `opencodeActive`,
   *  which is what every source meant before the two split. */
  readonly modelCatalog?: boolean;
  /** The permission modes a reader may PUT a chat into on this source.
   *
   *  Undefined means "every mode the product knows" — the editor, where the
   *  human owns the machine. An explicit list is a source narrowing itself to
   *  fewer, in the order its picker should read them. An EMPTY list is a source
   *  that fixes the mode; the pill then states `fixedPermissionMode` and offers
   *  no switch. */
  readonly permissionModes?: readonly string[];
  /** The reader may move an OPEN chat onto a different model.
   *
   *  Held apart from `modelCatalog` because serving a catalogue and being able
   *  to REPIN are different powers: a source can offer the picker on a brand-new
   *  chat and have nowhere to put a later switch. Where this is false the
   *  composer greys the picker on an existing chat rather than taking a pick it
   *  would silently drop — a chip that moves and a chat that does not is how a
   *  reader ends up reading one model's transcript under another's name. */
  readonly switchableModel?: boolean;
}

/** Whether this source serves a model catalogue. */
export function servesModels(caps: ChatCapabilities): boolean {
  return caps.modelCatalog ?? caps.opencodeActive;
}

/** Whether a reader may move an OPEN chat onto a different model on this
 *  source. Unset follows `opencodeActive`, which is what every source meant
 *  before the browser gained a catalogue it could also repin through. */
export function switchesModel(caps: ChatCapabilities): boolean {
  return caps.switchableModel ?? caps.opencodeActive;
}

/** The modes a reader may switch to on this source, as a set the surface can
 *  filter its option list by. Empty = the mode is not the reader's to change. */
export function switchableModes(caps: ChatCapabilities): ReadonlySet<string> {
  if (caps.permissionModes) return new Set(caps.permissionModes);
  return caps.opencodeActive ? EVERY_MODE : new Set<string>();
}

/** Every mode the product knows, for a source that narrows to none of them. */
const EVERY_MODE: ReadonlySet<string> = new Set(PERMISSION_MODE_VALUES);

/** What pressing Stop actually did.
 *
 *  A source that owns the running turn stops it and answers `stopped: true`.
 *  One that does not — the portal, whose turn is running on a machine it can
 *  only relay to — says so in `reason`, which the surface shows the reader. The
 *  composer is released either way: Stop always ends the reader's wait. */
export interface CancelOutcome {
  stopped: boolean;
  reason?: string;
}

/** What the machine running a turn says it is doing right now.
 *
 *  `working` means the turn is alive — the box is between a model step and its
 *  next visible event, which on a small machine is routinely longer than a
 *  minute. `idle` means no turn is under way. `null` means nobody has said,
 *  which is not the same as dead. */
export type TurnState = "working" | "idle";

export interface ChatDataSource {
  readonly caps: ChatCapabilities;

  /** A message this source ACCEPTS while a turn is running waits behind that
   *  turn rather than superseding it.
   *
   *  True of the cloud: the server records the prompt and answers at once, and
   *  the box's prompt lane holds it until the running turn goes idle. False —
   *  and so left unset — of the editor's daemon, where a prompt over a live
   *  turn supersedes it. The composer only tells a reader their message is
   *  queued on a source that really queues it. */
  readonly holdsSendsBehindTurn?: boolean;

  /** A message this source accepts is recorded first and started later, by a
   *  machine that takes the chat up.
   *
   *  True of the cloud: a message on the record that nothing has answered is
   *  owed a turn — after a reload as much as in the tab that sent it — and
   *  until the machine starts that turn the reader is told they are waiting
   *  for the workspace, not that the agent is working. Unset on the editor's
   *  daemon, which starts the turn in the same call that takes the message. */
  readonly startsTurnsRemotely?: boolean;

  /** The MACHINE's own word on the turn, where the source can hear it.
   *
   *  Absent on a source whose agent runs in the same process tree as the shell
   *  (the editor's daemon IS the machine, so its silence really is a death).
   *  The portal's source hears it on the chat document, which is what lets the
   *  stall watchdog tell a slow turn from a lost one instead of calling every
   *  quiet minute a crash. */
  turnState?(chatId: string): TurnState | null;

  // --- the shared composer draft -------------------------------------------
  //
  // Optional, because it is a property of a source whose chat has more than one
  // reader. The editor's daemon serves one person at one keyboard and its
  // composer is already the only copy; the portal's chat is a document several
  // people can have open, so what is half-typed in it belongs to the chat
  // rather than to the tab.

  /** Hold the chat's draft on the live (Loro) lane. It starts pending, goes
   *  live with a text binding, or falls back with the reason the lane could
   *  not run; the returned release lets go. */
  openLiveDraft?(chatId: string): { draft: LiveDraft; release: () => void };

  // --- chats ---------------------------------------------------------------
  listChats(): Promise<Chat[]>;
  createChat(initialMessage: string, opts?: SendOptions): Promise<Chat>;
  /** Open a chat with nothing said in it yet, titled for the message that is
   *  about to be its first: where a file must land in the chat before the
   *  message naming it can go. Absent on a source that can only open a chat
   *  by sending into it; the empty composer then offers no way to attach. */
  startChat?(title: string, opts?: SendOptions): Promise<Chat>;
  deleteChat(chatId: string, title?: string): Promise<boolean>;
  /** Re-open a chat whose harness session went away, so a refused send can be
   *  retried against a live session. */
  reopenChat(chatId: string): Promise<void>;
  sendUserMessage(chatId: string, content: string, opts?: SendOptions): Promise<ChatMessage>;
  getChatTurns(chatId: string): Promise<ChatTurn[]>;
  /** Live transcript events for one chat; the returned function unsubscribes. */
  subscribeChat(chatId: string, onEvent: (event: ChatEvent) => void): Unsubscribe;

  // --- the transcript window -----------------------------------------------
  //
  // Optional, because a source that serves whole transcripts has nothing above
  // what `getChatTurns` returns. A source that opens a chat on its newest page
  // says so here, and the panel asks for the pages above as the reader scrolls
  // up.

  /** Whether the durable record holds turns above the window, and whether a
   *  page is already on its way. */
  transcriptHistory?(chatId: string): { hasOlder: boolean; loading: boolean };
  /** Read one more page above the window; resolves once it is folded and
   *  announced (`history_loaded`). A no-op while nothing older exists or a
   *  page is in flight. */
  /** Read one page above the window. Resolves `false` when nothing came —
   *  no cursor to ask from, or a page that brought no new row — so a caller
   *  can tell a standstill from progress instead of asking again at once. */
  loadOlderTurns?(chatId: string): Promise<boolean | void>;
  /** The reader is back at the bottom: pages beyond the budget may go. */
  releaseOlderTurns?(chatId: string): void;

  // --- the chat list's derived state ---------------------------------------
  /** Per-chat live state folded from the events this session saw, keyed by chat id. */
  getChatActivity(): Record<string, ChatActivity>;
  /** Subagent (child) chats this session has observed. */
  getSubagentChats(): Chat[];
  /** Display label per subagent chat id, from the spawn card that started it. */
  getSubagentLabels(): Record<string, string>;

  // --- interrupts ----------------------------------------------------------
  // Answering an ask is a data-source verb, not a shell one: the extension
  // settles it on the local daemon's engine channel and the browser relays it
  // to the machine that raised it. The card that presents the ask is the same
  // component in both, and this is the seam under it.
  /** Answer a permission ask with one of the options the ask offered. */
  resolvePermission(chatId: string, requestId: string, optionId: string): Promise<void>;
  /** Answer a question ask: one list of chosen labels per prompt. */
  /** `note` is what the reader wrote for the model beside the answers; a source
   *  whose machine takes no note leaves it out. */
  answerQuestion(chatId: string, requestId: string, answers: string[][], note?: string | null): Promise<void>;
  /** Decline a question ask, optionally saying why. */
  rejectQuestion(chatId: string, requestId: string, reason: string | null): Promise<void>;
  /** Stop the turn in flight. Like answering an ask, this is a data-source
   *  verb, not a shell one: the editor cancels on the local daemon's engine
   *  channel and the portal has to reach the machine running the turn. It
   *  reports what it managed rather than throwing — a Stop that lands on a
   *  channel the shell does not have must never surface as an uncaught error. */
  cancelTurn(chatId: string): Promise<CancelOutcome>;
  /** Whether ALLOWING this ask could reach the agent, and what to say where it
   *  could not.
   *
   *  The machine discards an answer that would authorize a write on a session
   *  that refuses writes, so a surface that offered such an Allow would be a
   *  second control that lies. Declining is always offered — that is what
   *  releases the turn.
   *
   *  The chat is named because the answer depends on the stance THAT chat's
   *  session is in, and an ask carries no chat of its own: the same source
   *  follows every listed chat at once, so "the mode" is not a property of the
   *  source. A source whose sessions all answer the same way ignores it. */
  mayAllow(chatId: string, part: PermissionConversationPart): ApprovalVerdict;

  // --- engine options ------------------------------------------------------
  listModels(): Promise<ModelInfo[]>;
  resolveChatDefaults(): Promise<ChatDefaults>;
  setEffort(chatId: string, effort: string): Promise<void>;
  /** Move an open chat onto `model`. A source that pins the model once and
   *  cannot move it leaves this unimplemented — `switchableModel` says which,
   *  and the composer greys its picker accordingly. `expectedModelId` is the
   *  model the reader switched from: a chat someone else moved in between is
   *  refused rather than switched over their pick. A switch the chat may not
   *  make rejects with the reason as the error's message. */
  setModel?(
    chatId: string,
    model: string,
    effort?: string | null,
    expectedModelId?: string | null,
  ): Promise<void>;
  /** Every model this reader may pick for an open chat, each with whether the
   *  chat may move to it and why not. Implemented wherever `setModel` is. */
  modelOptions?(chatId: string): Promise<ModelOptions>;
  getPermissionMode(chatId: string): Promise<string | null>;
  setPermissionMode(chatId: string, mode: PermissionMode | string): Promise<void>;
  subscribePermissionMode(chatId: string, onMode: (mode: string) => void): Unsubscribe;

  // --- slash commands ------------------------------------------------------
  listCommands(): Promise<SlashCommandInfo[]>;
  runCommand(chatId: string, line: string): Promise<SlashCommandOutcome>;

  // --- the activity dock ---------------------------------------------------
  getCostState(chatId: string): Promise<CostStateView>;
  setCostLimits(chatId: string, caps: Record<string, number>): Promise<CostStateView>;
  listDecisions(
    chatId: string,
    opts?: { decidedBy?: string; limit?: number; offset?: number },
  ): Promise<{ decisions: DecisionView[]; hasMore: boolean }>;
  listCostLedger(
    chatId: string,
    opts?: { limit?: number; offset?: number },
  ): Promise<{ entries: CostLedgerEntryView[]; hasMore: boolean }>;
  getUsage(window: string): Promise<{ credits: Record<string, unknown>; usage: Record<string, unknown> }>;

  // --- references ----------------------------------------------------------
  fetchBlob(handle: string, offset?: number, limit?: number): Promise<BlobPage>;
  searchFiles(query: string): Promise<FileSearchResult[]>;
  /** The chat's own files: where a pasted image goes and how the transcript
   *  loads it back. Absent on a source with nowhere to put bytes — the
   *  composer then offers no paste and every chat image is a placeholder. */
  readonly chatFiles?: ChatFilesPort;

  // --- header counts -------------------------------------------------------
  /** A page of knowledge items; the chat header reads only its total. */
  listContext(opts?: { limit?: number }): Promise<{ total: number }>;
  /** The lineage roots summary; the chat header reads only its node total. */
  lineageRoots(limit?: number): Promise<{ total_nodes?: number }>;
}

/** What the chat's opening screen offers before there is a transcript.
 *
 *  The shell owns this because the copy is about the reader's world, not about
 *  the chat: the editor's reader has a repository open and is asked what to
 *  build in it; a browser reader has a warehouse and is asked what to find out.
 *  The composition shipped the editor's wording to both, so an analytics
 *  prospect's first screen offered to write tests "for the file I have open" —
 *  a file a browser reader does not have. */
export interface ChatEmptyState {
  /** The one line above the suggestions. */
  title: string;
  /** The suggestions themselves; picking one sends its prefill as the turn. */
  prompts: SuggestedPrompt[];
  /** What the empty composer invites, where the shell's reader is asked for
   *  something different from the editor's. The portal's reader has data in
   *  front of them, not a repository, and being told to "plan, build, or run
   *  something" is the coding agent's voice in a product about answers. The
   *  editor leaves this out and keeps the composer's own default. */
  placeholder?: string;
}

/** A document the chat hands its shell to display read-only (a plan). */
export interface ChatDocumentRequest {
  title: string;
  markdown: string;
}

/** A message pushed from the shell into the webview/page (`{type, args}`). */
export interface ChatHostMessage {
  type: string;
  args?: Record<string, unknown>;
}

/** Who the shell says is signed in, and where the web app lives. Both are null
 *  until the shell knows: the chrome renders neither the account line nor the
 *  door to the web app on a guess. */
export interface ChatAccount {
  email: string | null;
  webAppUrl: string | null;
  /** The signed-in user's id, where the shell knows it (the browser). What this
   *  browser remembers about org data is filed under it; a shell that leaves it
   *  out is keyed by `email` instead. */
  userId?: string | null;
  /** The org the session is acting in, where the shell knows it (the browser).
   *  Left out, remembered values are keyed with no org. */
  orgId?: string | null;
}

/**
 * The shell services the chat composition needs that are not data: opening a
 * file in an editor, running an IDE command, saving an export. The VS Code
 * webview's `HostAdapter` satisfies this structurally; the browser installs a
 * small adapter that downloads instead of writing and no-ops the editor verbs.
 */
export interface ChatHost {
  readonly kind: "browser" | "vscode";
  runCommand(payload: { command: string; args?: Record<string, unknown> }): Promise<void>;
  /** Open a file the agent named, where the shell has somewhere to open it.
   *
   *  Absent on a shell that has no editor. That absence is load-bearing: a tool
   *  card renders a path as a button only when this is wired, so the browser
   *  shows the path as the label it is instead of a control that answers a
   *  click with nothing. A no-op implementation would have looked identical to
   *  the code and been a dead control on screen. */
  openFile?(path: string): Promise<void>;
  /** Absolute workspace root, or null where the shell has no workspace. */
  workspacePath(): string | null;
  /** Save `contents` where the user chooses — a browser download, a native dialog. */
  saveFile(suggestedName: string, contents: string, mimeType?: string): Promise<void>;
  /** Show a plan as a read-only document; the browser keeps its own page instead. */
  openPlanDocument(request: ChatDocumentRequest): Promise<void>;
  /** What this shell's opening screen asks, where it has something of its own
   *  to ask. Absent leaves the composition's own (the editor's). */
  readonly emptyState?: ChatEmptyState;
  subscribe(handler: (message: ChatHostMessage) => void): () => void;
  /** The signed-in account as the shell currently knows it. */
  account(): ChatAccount;
  /** Fires whenever `account()` would answer differently; returns an unsubscribe. */
  onAccountChange(listener: () => void): () => void;
  auth: { openBrowser(url: string): void };
  /** The shell's high-level engine channel (VS Code only in practice). */
  engine: { request<T = unknown>(op: string, payload?: unknown): Promise<T> };
}

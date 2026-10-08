// One live document on the socket: the browser's side of the Loro lane.
//
// The channel holds the tab's copy of the document (a LoroDoc) and keeps it in
// step with the server's, which is the authority:
//
// * It subscribes first and loads Loro only once the server has granted the
//   channel — a refused subscribe never pays for the WebAssembly.
// * `hello` carries the version vector it holds; the server answers with what
//   it lacks (or the whole document). The copy survives reconnects: a dropped
//   socket costs a hello, never the text.
// * Local edits go up as ONE update at a time: whatever was typed while one was
//   in flight is coalesced into the next. An edit is settled when the server's
//   vector in the `ack` covers it — the vector after a durable commit.
// * Missing history (an import that reports `pending`), an ack that never
//   comes, or a quiet half minute all end the same way: hello again with the
//   vector, and only the difference travels.
// * A new epoch (the history restarted) rebases this tab's unacknowledged
//   edits onto the new document. A refused update resets the copy to the
//   server's and offers the person their text back. Nothing else ever throws
//   away an edit the server has not acknowledged: busy, a timeout, a closed
//   socket and a lost ack all keep the copy and send it again, and a rebuild
//   the type cannot carry an edit across (a notebook's new epoch) offers it
//   back too. A channel the server will not serve (`crdt_unsupported`, a
//   refused subscribe) or a Loro that will not load ends the channel in
//   `fallback`, carrying the text it held and what of it the server never took.
// * Unacknowledged edits outlive the page: on `pagehide` they are kept in the
//   tab's session store under the signed-in account, and the next page on the
//   same document sends them (same epoch) or carries them as one splice (a new
//   epoch, or a server that never got to them). They stay in the store until
//   the server has answered for them, so a second reload loses nothing either.
//   An offer made while nobody listens for one is held until somebody does,
//   and `holdsUnacknowledged` says whether the channel may be let go.

import type { ClientFrame, EnvelopeKind, ServerFrame, SocketLimits } from "../wsClient";
import { spliceFor } from "@alkera/ui";
import { accountKey, safeSessionStorage, type SafeStorage } from "@alkera/ui/storage";

import type { AccountScope } from "../../../lib/accountScope";
import { LIVE_BUSY_RETRY, LIVE_OPEN_DEADLINE, RETRY_JITTER_RATIO, ladderDelay } from "../../../lib/limits";

import { fromBase64, toBase64 } from "./bytes";
import { CHUNK_BYTES, ChunkAssembler, isChunk, split } from "./chunks";
import { LIVE_DOC_TYPES, type LiveDocType } from "./docTypes";
import { LORO_JS_VERSION, type LoroApi } from "./loro";
import { budgetFor, type SendBudget } from "./sendBudget";

type LoroDoc = InstanceType<LoroApi["LoroDoc"]>;
type VersionVector = InstanceType<LoroApi["VersionVector"]>;

/** The lane's wire protocol (the server's `CRDT_PROTOCOL`). */
export const CRDT_PROTOCOL = 1;
/** The document schema this build understands. */
export const DOC_SCHEMA = 1;
/** An ack that has not come by then is presumed lost: resync. */
export const ACK_TIMEOUT_MS = 15_000;
/** A quiet channel re-checks its vector this often, to catch a missed broadcast. */
export const IDLE_RESYNC_MS = 30_000;
/** A hello not answered by then is asked again (the first rung of
 *  `LIVE_OPEN_DEADLINE`; later asks wait longer). */
export const HELLO_TIMEOUT_MS = LIVE_OPEN_DEADLINE.floorMs;
/** Why a channel stopped trying to open: the server never answered its
 *  subscribe or its hello, however often it asked. The host offers a retry. */
export const NO_ANSWER = "no_answer";
/** Carets go out at most this often. */
export const EPHEMERAL_INTERVAL_MS = 100;
/** How long an edit may wait for the server before the tab says it is not
 *  saved yet (and is still trying). */
export const UNSAVED_AFTER_MS = 3_000;
/** A refused subscribe the server may grant on a later try (it was busy, or
 *  failed for a moment): asked again with backoff, the tab's copy kept. */
const TRANSIENT_REFUSALS = new Set(["internal", "unavailable", "busy", "crdt_busy", "rate_limited", "db_lock_timeout", "timeout"]);

/** How long to wait after the server answered busy for the `rung`th time in a
 *  row: what it asked for, or the ladder's rung when that is longer, plus a
 *  share of it at random so tabs refused together do not return together. */
export function busyWait(rung: number, askedMs: number | null, random: () => number): number {
  const climb = ladderDelay(rung, LIVE_BUSY_RETRY);
  const base = askedMs === null ? climb : Math.max(askedMs, climb);
  return Math.round(base * (1 + RETRY_JITTER_RATIO * random()));
}

/** How long the channel waits on the `unanswered`th ask in a row (a subscribe
 *  or a hello) before it asks again, with a share added at random so tabs that
 *  lost their answers together do not ask again together. */
export function answerDeadline(unanswered: number, random: () => number): number {
  return Math.round(ladderDelay(unanswered, LIVE_OPEN_DEADLINE) * (1 + RETRY_JITTER_RATIO * random()));
}

/** What the channel needs of the socket (a WsClient). */
export interface LiveSocket {
  readonly peerId: string | null;
  readonly limits: SocketLimits | null;
  subscribe(channel: string): () => void;
  send(frame: ClientFrame): boolean;
  onFrame(listener: (frame: ServerFrame) => void): () => void;
  onOpen(listener: (peerId: string) => void): () => void;
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const DEFAULT_TIMERS: Timers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export type LivePhase = "connecting" | "live" | "fallback" | "closed";

/** Why the channel stopped serving the document, and the text it held. */
export interface LiveFallback {
  reason: string;
  /** The server's finer reason, when it gave one: why a document it will not
   *  open live cannot be (a notebook's `not_a_notebook`, `unreadable` or
   *  `newer_format`). */
  detail?: string;
  /** What this tab shows. */
  localText: string;
  /** What the server last acknowledged (the base a three-way merge edits from). */
  ackedText: string;
  /** What this tab typed that the server never took, as the type reads it
   *  (`LiveChannelOptions.unacknowledged`): the text a host that cannot keep
   *  showing `localText` offers back. Empty when nothing is unacknowledged. */
  unacknowledged: string;
}

/** Who moved a caret, as the server stamped it. */
export interface EphemeralStamp {
  loroPeer: number;
  userId: string;
  displayName: string;
  email: string;
}

export interface LiveNotice {
  message: string;
  restorable?: string;
}

export interface LiveListeners {
  /** Before Loro applies somebody else's change (capture where the caret is). */
  beforeRemote?(): void;
  /** After it (show the new text, put the caret back). */
  afterRemote?(): void;
  /** The document object itself changed (a new epoch, a reset). */
  replaced?(doc: LoroDoc): void;
  phase?(phase: LivePhase, fallback?: LiveFallback): void;
  ephemeral?(data: Uint8Array, stamp: EphemeralStamp): void;
  /** The server says a tab left: its Loro peer's caret should go. */
  gone?(loroPeer: number): void;
  notice?(notice: LiveNotice | null): void;
  /** Whether this tab may write, as the server says. */
  writable?(canWrite: boolean): void;
  /** Whether the document's edits are reaching where it rests (a file's
   *  drive): why not, or `null` once they are again. */
  saving?(paused: LiveSavingPaused | null): void;
  /** Whether this tab holds edits the server has not taken for longer than
   *  :data:`UNSAVED_AFTER_MS` (it keeps them, and keeps trying). */
  unsaved?(unsaved: boolean): void;
}

/** The server cannot write the document back: `no_writer` (nobody in it may
 *  still write the file), `leased` (a machine holds the folder and takes no
 *  outside write), `gone` (the file is in the trash), `too_large` / `binary`
 *  / `text_too_large` (the file on the drive can no longer be edited live),
 *  `quarantined_lost` (a broken history lost the edits since the last save),
 *  or the drive's refusal code (`files.frozen`, `files.quota_*`). */
export interface LiveSavingPaused {
  reason: string;
}

export interface LiveChannelOptions {
  socket: LiveSocket;
  loadLoro: () => Promise<LoroApi>;
  docType: LiveDocType;
  docId: string;
  timers?: Timers;
  budget?: SendBudget;
  /** Fresh ids for updates and transfers. */
  newId?: () => string;
  /** The jitter on every wait after a busy answer (`Math.random` by default). */
  random?: () => number;
  /** Where edits the server had not acknowledged when the page went away are
   *  kept for the next page in this tab (sessionStorage by default). */
  storage?: UnloadStash | null;
  /** Who is signed in and in which org. The stash is kept under them, so a page
   *  in another account or org is never handed these edits. Required, so no
   *  document type leaves its edits behind with the page by omission; only a
   *  channel that genuinely knows nobody (a dev harness) names `null`, and
   *  keeps no stash. */
  account: AccountScope | null;
  /** What says the page is going away (the window by default). */
  pageEvents?: PageEvents | null;
  /** What says the page is shown again (the document by default): a channel
   *  still opening asks at once, rather than on a timer a hidden tab slowed. */
  visibility?: PageVisibility | null;
  /** The text this tab would lose if its copy were replaced: what `local`
   *  holds that `acked` (the server's confirmed part of it, `null` when
   *  nothing is confirmed) does not, and `kept` (the copy replacing it, when
   *  known) does not hold either. Offered back on a reset. By default, for a
   *  type with one content text, the text typed since the last
   *  acknowledgement; a type whose content is not one text (a notebook)
   *  names its own. */
  unacknowledged?: Unacknowledged;
}

export type Unacknowledged = (acked: LoroDoc | null, local: LoroDoc, kept: LoroDoc | null) => string;

export type UnloadStash = Pick<SafeStorage, "get" | "set" | "remove">;
export type PageEvents = Pick<Window, "addEventListener" | "removeEventListener">;
/** What says whether the page is shown (the document by default). */
export type PageVisibility = Pick<Document, "hidden" | "addEventListener" | "removeEventListener">;

/** What the person is told when edits they typed could not be kept in the
 *  document (a restarted history could not carry them, or the document stopped
 *  being editable live); offered with the text. */
export const EDITS_NOT_KEPT = "Your latest edits could not be kept.";

/** How often an earlier page's unsent edits are sent as they were written
 *  before they are carried as a fresh edit instead. */
const REPLAY_TRIES = 10;
/** The server's refusals of the edits themselves (they are not this person's
 *  to make, or it will not take them): anything else it answers is asked again. */
const REPLAY_REFUSALS = new Set(["crdt_rejected", "forbidden"]);
/** Answers that the next sync settles (the epoch moved, or the server does not
 *  know this socket's copy yet): the replay goes again after it. */
const REPLAY_AFTER_SYNC = new Set(["not_synced", "stale_epoch", "crdt_resync"]);

/** The key family a channel's unsent edits are kept under. */
export const UNLOAD_STASH_FAMILY = "crdt.unsent";

/** Where a channel keeps its unsent edits: the account key, then the channel
 *  name. A stash written before keys named the org is ignored, not migrated:
 *  it lives ten minutes in one tab, and an old one is simply never replayed. */
export function unloadStashKey(scope: AccountScope, channel: string): string {
  return `${accountKey(scope.userId, scope.orgId, UNLOAD_STASH_FAMILY)}:${channel}`;
}

/** A doc type's spelling on an earlier build: a page on that build stashed
 *  its unsent edits under the channel name it used then. */
const EARLIER_SPELLING: Partial<Record<LiveDocType, string>> = { chat_draft: "chat_workspace" };

/** A stash older than this is from a visit nobody is coming back to. */
export const UNLOAD_STASH_TTL_MS = 10 * 60_000;


interface Inflight {
  id: string;
  upto: VersionVector;
  timer: unknown;
}

/** One edit to a text, as it can be made again on another copy: where, the
 *  text it took out, and the text it put in. */
export interface CarriedEdit {
  index: number;
  removed: string;
  insert: string;
}

/** What a page keeps for the next one: the update (the same epoch replays it)
 *  and, to carry it onto another history, the edit it made as one splice (a
 *  text type) or what it would offer back (any other). The splice, not the
 *  texts around it: a megabyte file must still fit in the tab's store. */
interface StashedEdits {
  epoch: number;
  at: number;
  data_b64: string;
  edit?: CarriedEdit;
  offer?: string;
}

function isCarriedEdit(value: unknown): value is CarriedEdit {
  if (typeof value !== "object" || value === null) return false;
  const { index, removed, insert } = value as Record<string, unknown>;
  return typeof index === "number" && Number.isSafeInteger(index) && index >= 0 && typeof removed === "string" && typeof insert === "string";
}

/** What `carrySplice` edits: a Loro text, or anything shaped like one. */
export interface SplicedText {
  toString(): string;
  insert(at: number, text: string): void;
  delete(at: number, length: number): void;
}

/** Carry the edit that took `base` to `local` onto `text` (another history's
 *  copy) as one splice: an edit `text` already holds is not typed twice, and
 *  nothing is deleted that this tab did not see there. Whether it changed. */
export function carrySplice(text: SplicedText, base: string, local: string): boolean {
  const edit = editBetween(base, local);
  if (edit === null || text.toString() === local) return false;
  return carryEdit(text, edit);
}

/** The edit that took `base` to `local`, as one splice; `null` when nothing did. */
export function editBetween(base: string, local: string): CarriedEdit | null {
  const splice = spliceFor(base, local);
  if (splice === null) return null;
  return { index: splice.index, removed: base.slice(splice.index, splice.index + splice.remove), insert: splice.insert };
}

/** Make `edit` again on `text`, under the same two rules as `carrySplice`. */
export function carryEdit(text: SplicedText, edit: CarriedEdit): boolean {
  const shown = text.toString();
  // An edit the server committed but never acknowledged (the ack was lost
  // with the socket) is already in the new document: carrying it again
  // would type it twice.
  if (edit.insert !== "" && shown.slice(edit.index, edit.index + edit.insert.length) === edit.insert) return false;
  const at = Math.min(edit.index, shown.length);
  // Delete only what this tab saw and the new document still holds there.
  const remove = edit.removed !== "" && shown.slice(at, at + edit.removed.length) === edit.removed ? edit.removed.length : 0;
  if (remove === 0 && !edit.insert) return false;
  if (remove > 0) text.delete(at, remove);
  if (edit.insert) text.insert(at, edit.insert);
  return true;
}

function randomId(): string {
  return globalThis.crypto?.randomUUID?.().replace(/-/g, "").slice(0, 24) ?? String(Math.random()).slice(2);
}

export class LiveDocChannel {
  readonly channel: string;
  /** The root text the document keeps its content in (its type's), or null
   *  for a type whose content is not one text (a notebook keeps a text per
   *  cell, and its editors bind those). */
  readonly textName: string | null;
  private readonly docType: LiveDocType;
  private readonly docId: string;
  private readonly socket: LiveSocket;
  private readonly loadLoro: () => Promise<LoroApi>;
  private readonly timers: Timers;
  private readonly budget: SendBudget;
  private readonly newId: () => string;
  private readonly listeners = new Set<LiveListeners>();
  private readonly snapshots = new ChunkAssembler();
  private readonly updates = new ChunkAssembler();

  private loro: LoroApi | null = null;
  private _doc: LoroDoc | null = null;
  private _phase: LivePhase = "connecting";
  private _canWrite = false;
  private _savingPaused: LiveSavingPaused | null = null;
  private _unsaved = false;
  private unsavedTimer: unknown = null;
  private resubscribeTimer: unknown = null;
  /** Busy answers in a row, the rung every retry waits on; back to 0 once
   *  the server answers anything else. */
  private busyRung = 0;
  private readonly random: () => number;
  private epoch = 0;
  private loroPeer: number | null = null;
  private serverVV: VersionVector | null = null;
  private inflight: Inflight | null = null;
  private retryTimer: unknown = null;
  private idleTimer: unknown = null;
  /** The deadline on the answer the channel waits for (to its subscribe, then
   *  to its hello); past it, the channel asks again. */
  private helloTimer: unknown = null;
  /** The server has answered this socket's subscribe. */
  private subscribed = false;
  /** Asks in a row the server never answered while the page was shown. */
  private unanswered = 0;
  private resyncAfterSync = false;
  private ephemeralTimer: unknown = null;
  private pendingEphemeral: Uint8Array | null = null;
  private lastEphemeralAt = 0;
  private release: (() => void) | null = null;
  private unlisten: (() => void)[] = [];
  private hellos = 0;
  private readonly stash: UnloadStash | null;
  private readonly stashKey: string;
  /** Where a page on an earlier build stashed this doc's edits, if anywhere else. */
  private readonly earlierStashKey: string | null;
  private readonly pageEvents: PageEvents | null;
  private readonly visibility: PageVisibility | null;
  private readonly unacknowledgedOf: Unacknowledged | null;
  private replayed = false;
  /** The update carrying an earlier page's unsent edits, until the server
   *  takes or refuses it. */
  private replay: { id: string; data: Uint8Array; epoch: number; tries: number; held: StashedEdits } | null = null;
  /** Offers of text (a notice with `restorable`) made while no listener took
   *  notices: handed to the next one that does. */
  private readonly heldNotices: LiveNotice[] = [];
  /** The tab's store holds a stash for this document (this page wrote it, or
   *  is still answering for an earlier page's): removed once nothing here
   *  waits on the server. */
  private stashed = false;

  constructor(opts: LiveChannelOptions) {
    this.channel = `doc:${opts.docType}:${opts.docId}`;
    this.textName = LIVE_DOC_TYPES[opts.docType].text;
    this.docType = opts.docType;
    this.docId = opts.docId;
    this.socket = opts.socket;
    this.loadLoro = opts.loadLoro;
    this.timers = opts.timers ?? DEFAULT_TIMERS;
    this.budget = opts.budget ?? budgetFor(opts.socket);
    this.newId = opts.newId ?? randomId;
    this.random = opts.random ?? Math.random;
    this.stash = opts.account ? (opts.storage === undefined ? safeSessionStorage() : opts.storage) : null;
    this.stashKey = opts.account ? unloadStashKey(opts.account, this.channel) : "";
    const spelled = EARLIER_SPELLING[opts.docType];
    this.earlierStashKey =
      opts.account && spelled !== undefined ? unloadStashKey(opts.account, `doc:${spelled}:${opts.docId}`) : null;
    this.pageEvents =
      opts.pageEvents === undefined ? ((globalThis as { window?: PageEvents }).window ?? null) : opts.pageEvents;
    this.visibility =
      opts.visibility === undefined ? ((globalThis as { document?: PageVisibility }).document ?? null) : opts.visibility;
    this.unacknowledgedOf = opts.unacknowledged ?? null;
  }


  get phase(): LivePhase {
    return this._phase;
  }

  get canWrite(): boolean {
    return this._canWrite;
  }

  /** Whether edits typed here have waited on the server past the bound. */
  get unsaved(): boolean {
    return this._unsaved;
  }

  /** Why the document's edits are not being saved, or `null` when they are. */
  get savingPaused(): LiveSavingPaused | null {
    return this._savingPaused;
  }

  get doc(): LoroDoc | null {
    return this._doc;
  }

  get peer(): number | null {
    return this.loroPeer;
  }

  /** The epoch of the document this tab holds (0 before the first sync). */
  get docEpoch(): number {
    return this.epoch;
  }

  get limits(): { maxTextBytes: number; maxUpdateBytes: number } {
    return { maxTextBytes: this.maxTextBytes, maxUpdateBytes: this.maxUpdateBytes };
  }

  private maxTextBytes = 16 * 1024;
  private maxUpdateBytes = 512 * 1024;

  listen(listeners: LiveListeners): () => void {
    this.listeners.add(listeners);
    if (listeners.notice !== undefined && this.heldNotices.length > 0) {
      for (const notice of this.heldNotices.splice(0)) listeners.notice(notice);
    }
    return () => void this.listeners.delete(listeners);
  }

  /** Whether letting this channel go would lose something the person typed:
   *  edits the server has not acknowledged (or an earlier page's, still being
   *  offered to it), or an offer of text nobody has been shown yet. A holder
   *  that releases the channel keeps it while this is true. */
  get holdsUnacknowledged(): boolean {
    if (this.heldNotices.length > 0) return true;
    if (this._phase === "fallback" || this._phase === "closed") return false;
    return this.replay !== null || (this._canWrite && this.ahead());
  }

  /** Show `notice` to whoever listens for notices; an offer of text nobody
   *  listens for yet is held for the first who does, never dropped. */
  private offer(notice: LiveNotice): void {
    const listening = [...this.listeners].some((l) => l.notice !== undefined);
    if (!listening && notice.restorable) {
      this.heldNotices.push(notice);
      return;
    }
    this.emit("notice", notice);
  }

  private emit<K extends keyof LiveListeners>(
    key: K,
    ...args: Parameters<NonNullable<LiveListeners[K]>>
  ): void {
    for (const l of [...this.listeners]) {
      const fn = l[key] as ((...a: typeof args) => void) | undefined;
      fn?.(...args);
    }
  }

  start(): void {
    if (this.release !== null || this._phase === "closed") return;
    this.unlisten.push(
      this.socket.onFrame((frame) => {
        // A frame this copy cannot read is answered by asking again from what
        // it holds, never by stopping: the next sync replaces the doubt.
        this.onFrame(frame).catch(() => this.resync());
      }),
    );
    this.unlisten.push(
      this.socket.onOpen(() => {
        // A new socket: whatever was in flight on the old one is unknowable
        // until the next sync says what the server holds. The socket has
        // subscribed this channel again; its answer brings the hello, and the
        // deadline asks again if that answer is lost too.
        this.clearInflight();
        this.subscribed = false;
        this.unanswered = 0;
        this.armDeadline();
      }),
    );
    this.release = this.socket.subscribe(this.channel);
    this.armDeadline();
    const shown = this.visibility;
    if (shown !== null) {
      const onShown = (): void => {
        if (!shown.hidden) this.shownAgain();
      };
      shown.addEventListener("visibilitychange", onShown);
      this.unlisten.push(() => shown.removeEventListener("visibilitychange", onShown));
    }
    const events = this.pageEvents;
    if (events !== null) {
      const onHide = (): void => this.unloading();
      events.addEventListener("pagehide", onHide);
      this.unlisten.push(() => events.removeEventListener("pagehide", onHide));
    }
  }

  /** The page is going away with edits the server has not acknowledged:
   *  typed while an update was in flight, or the update itself. They are sent
   *  now, past the one-in-flight rule (the server takes an operation it
   *  already holds as a no-op), and kept for the next page in case the socket
   *  closes before they leave. An earlier page's edits this one is still
   *  offering the server are kept with them. */
  private unloading(): void {
    const doc = this._doc;
    if (doc === null || this.serverVV === null || this._phase !== "live" || !this._canWrite) return;
    if (!this.ahead()) return;
    const update = doc.export({ mode: "update", from: this.serverVV });
    const both = this.withReplay(doc);
    this.keepForNextPage(both?.update ?? update, both?.text ?? this.localText(), both?.offer ?? "");
    if (update.length <= CHUNK_BYTES) {
      this.socket.send(this.envelope("crdt", { t: "update", update_id: this.newId(), data_b64: toBase64(update) }));
    }
  }

  /** This copy's unacknowledged operations together with an earlier page's
   *  the server has not answered for: one update, the text the two make, and
   *  what the earlier page would offer back. `null` when there are none of
   *  the latter (or they cannot be read with this copy). */
  private withReplay(doc: LoroDoc): { update: Uint8Array; text: string; offer: string } | null {
    const replay = this.replay;
    if (replay === null || replay.epoch !== this.epoch || this.serverVV === null) return null;
    try {
      const both = doc.fork();
      both.import(replay.data);
      return {
        update: both.export({ mode: "update", from: this.serverVV }),
        text: this.textName === null ? "" : both.getText(this.textName).toString(),
        offer: replay.held.offer ?? "",
      };
    } catch {
      return null;
    }
  }

  /** Keep `update`, and what carrying it onto another history needs (the edit
   *  that made `text`, or `earlierOffer` and this copy's own), in the tab's
   *  session store for the next page. A store too full for that keeps the
   *  update alone, which the same epoch can still take. */
  private keepForNextPage(update: Uint8Array, text: string, earlierOffer: string): void {
    if (this.stash === null) return;
    const bare: StashedEdits = { epoch: this.epoch, at: Date.now(), data_b64: toBase64(update) };
    const held: StashedEdits = { ...bare };
    if (this.textName !== null) {
      const edit = editBetween(this.ackedText(), text);
      if (edit !== null) held.edit = edit;
    } else {
      held.offer = [earlierOffer, this.unacknowledged(null)].filter((part) => part !== "").join("\n\n");
    }
    const kept = this.stash.set(this.stashKey, JSON.stringify(held)) || this.stash.set(this.stashKey, JSON.stringify(bare));
    this.stashed = this.stashed || kept;
  }

  /** What an earlier page in this tab could not send, read on the first sync
   *  (which then sends it).
   *  The stash is filed under the person and the org, so it is this reader's
   *  own: the same epoch is sent its operations as they were written, and
   *  another epoch (the history restarted between the pages) is carried the
   *  edit they made. It stays in the store until the server has answered. */
  private replayStash(): void {
    if (this.replayed) return;
    this.replayed = true;
    const held = this.readStash();
    if (held === null) return;
    // A reader who may no longer write here is refused before asking.
    if (!this._canWrite) {
      this.dropStash();
      return;
    }
    this.stashed = true;
    if (held.epoch !== this.epoch) {
      this.carry(held);
      return;
    }
    let data: Uint8Array;
    try {
      data = fromBase64(held.data_b64);
    } catch {
      this.dropStash();
      return;
    }
    this.replay = { id: this.newId(), data, epoch: this.epoch, tries: 0, held };
  }

  /** The stash an earlier page left for this document, when it is one this
   *  page can use; anything else (unreadable, or from a visit nobody is coming
   *  back to) is removed. */
  private readStash(): StashedEdits | null {
    const earlier = this.earlierStashKey;
    const raw = this.stash?.get(this.stashKey) ?? (earlier === null ? null : this.stash?.get(earlier)) ?? null;
    if (raw === null) return null;
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = null;
    }
    const held = (typeof parsed === "object" && parsed !== null ? parsed : {}) as Partial<Record<keyof StashedEdits, unknown>>;
    if (
      typeof held.epoch !== "number" ||
      typeof held.data_b64 !== "string" ||
      typeof held.at !== "number" ||
      Date.now() - held.at > UNLOAD_STASH_TTL_MS
    ) {
      this.dropStash();
      return null;
    }
    return {
      epoch: held.epoch,
      at: held.at,
      data_b64: held.data_b64,
      ...(isCarriedEdit(held.edit) ? { edit: held.edit } : {}),
      ...(typeof held.offer === "string" ? { offer: held.offer } : {}),
    };
  }

  private dropStash(): void {
    this.stash?.remove(this.stashKey);
    if (this.earlierStashKey !== null) this.stash?.remove(this.earlierStashKey);
    this.stashed = false;
  }

  /** An earlier page's edits whose operations this document cannot take as
   *  they were written (its history restarted, or the server never got to
   *  them): a text type makes the edit again as one splice, as a new epoch
   *  reached while the page was open does; a type that cannot be spliced
   *  offers back what they held. */
  private carry(held: StashedEdits): void {
    const doc = this._doc;
    if (doc === null) return;
    if (this.textName === null) {
      if (held.offer) this.offer({ message: EDITS_NOT_KEPT, restorable: held.offer });
      return;
    }
    if (held.edit === undefined) return;
    this.emit("beforeRemote");
    const carried = carryEdit(doc.getText(this.textName), held.edit);
    if (carried) doc.commit({ origin: "local" });
    this.emit("afterRemote");
    if (!carried) return;
    this.watchUnsaved();
    this.push();
  }

  private async sendReplay(): Promise<void> {
    const replay = this.replay;
    if (replay === null || this._phase === "closed" || this._phase === "fallback") return;
    // Its operations belong to the epoch they were written in. A new one
    // replaced that history, but not what they typed.
    if (replay.epoch !== this.epoch) {
      this.replay = null;
      this.carry(replay.held);
      this.settled();
      return;
    }
    replay.tries += 1;
    if (replay.data.length <= CHUNK_BYTES) {
      this.socket.send(
        this.envelope("crdt", { t: "update", update_id: replay.id, data_b64: toBase64(replay.data) }, replay.epoch),
      );
      return;
    }
    for (const chunk of await split(replay.data, replay.id)) {
      if (this.replay !== replay) return;
      this.socket.send(this.envelope("crdt", { t: "update", update_id: replay.id, chunk }, replay.epoch));
    }
  }

  /** The server's answer to the replayed edits, when `id` names them. Taken,
   *  they come back like any other edit. Refused (they are not this reader's
   *  to make), they are dropped. Anything else is asked again, and a server
   *  that never gets to them is handed the edit afresh instead. */
  private settleReplay(id: unknown, outcome: "ack" | "error", code = "", retryAfterMs: number | null = null): boolean {
    const replay = this.replay;
    if (replay === null || id !== replay.id) return false;
    if (outcome === "error" && !REPLAY_REFUSALS.has(code)) {
      if (replay.tries >= REPLAY_TRIES) {
        this.replay = null;
        this.carry(replay.held);
        this.settled();
      } else if (REPLAY_AFTER_SYNC.has(code)) {
        // Every sync sends it again; a new epoch's `reload` brings its own.
        if (code !== "stale_epoch") this.resync();
      } else {
        this.timers.setTimeout(() => void this.sendReplay(), this.nextBusyWait(retryAfterMs));
      }
      return true;
    }
    this.replay = null;
    if (outcome === "ack") this.resync();
    else this.settled();
    return true;
  }

  close(): void {
    if (this._phase === "closed") return;
    this.setPhase("closed");
    this.clearInflight();
    for (const timer of [
      this.retryTimer,
      this.idleTimer,
      this.ephemeralTimer,
      this.helloTimer,
      this.unsavedTimer,
      this.resubscribeTimer,
    ]) {
      if (timer !== null) this.timers.clearTimeout(timer);
    }
    this.retryTimer = this.idleTimer = this.ephemeralTimer = this.helloTimer = null;
    this.unsavedTimer = this.resubscribeTimer = null;
    this.unlisten.forEach((u) => u());
    this.unlisten = [];
    this.release?.();
    this.release = null;
  }

  // -- the text the server last confirmed ---------------------------------

  /** The text as the server last acknowledged it: the base a fallback or a
   *  rebase edits from. */
  ackedText(): string {
    if (this.textName === null) return "";
    return this.ackedDoc()?.getText(this.textName).toString() ?? "";
  }

  /** This copy as far as the server confirmed it; `null` when nothing is
   *  (every edit here is then offered back, the safe reading). */
  private ackedDoc(): LoroDoc | null {
    const doc = this._doc;
    const loro = this.loro;
    if (doc === null) return null;
    if (this.serverVV === null || loro === null) return doc;
    // The server may hold operations this tab has not received yet (an ack
    // can outrun the broadcast it covers): what it confirmed of THIS copy is
    // the part of its version this copy also holds.
    const held = doc.oplogVersion();
    const confirmed = new Map<`${number}`, number>();
    for (const [peer, end] of this.serverVV.toJSON()) {
      const mine = held.get(peer) ?? 0;
      if (Math.min(end, mine) > 0) confirmed.set(peer, Math.min(end, mine));
    }
    try {
      return doc.forkAt(doc.vvToFrontiers(new loro.VersionVector(confirmed)));
    } catch {
      return null;
    }
  }

  /** What replacing this copy (with `kept`, when known) would take from the
   *  reader: see `LiveChannelOptions.unacknowledged`. */
  private unacknowledged(kept: LoroDoc | null): string {
    const doc = this._doc;
    if (doc === null) return "";
    if (this.unacknowledgedOf !== null) return this.unacknowledgedOf(this.ackedDoc(), doc, kept);
    if (this.textName === null) return "";
    return spliceFor(this.ackedText(), this.localText())?.insert ?? "";
  }

  private localText(): string {
    return this.textName === null ? "" : (this._doc?.getText(this.textName).toString() ?? "");
  }

  // -- outbound ------------------------------------------------------------

  private envelope(kind: EnvelopeKind, payload: Record<string, unknown>, epoch = this.epoch): ClientFrame {
    const { docType, docId } = this;
    return {
      t: "doc",
      envelope: { doc_id: docId, doc_type: docType, epoch, peer_id: this.socket.peerId ?? "", seq: 0, kind, payload },
    };
  }

  private hello(): void {
    if (this._phase === "closed" || this._phase === "fallback") return;
    this.hellos += 1;
    this.armDeadline();
    const payload: Record<string, unknown> = { proto: CRDT_PROTOCOL, loro: LORO_JS_VERSION, doc_schema: DOC_SCHEMA };
    if (this._doc !== null && this.epoch > 0) {
      payload.vv_b64 = toBase64(this._doc.oplogVersion().encode());
      payload.epoch_seen = this.epoch;
      if (this.loroPeer !== null) payload.loro_peer = this.loroPeer;
    }
    this.socket.send(this.envelope("hello", payload, this._doc !== null ? this.epoch : 0));
  }

  /** Wait for the answer the channel is asking for (its subscribe's, then its
   *  hello's), each ask a rung longer than the last. Whatever stalls it (a lost
   *  frame, a socket that restarted mid-hello, a server stuck behind a lock),
   *  an unanswered ask is asked again rather than leaving the tab connecting. */
  private armDeadline(): void {
    if (this._phase === "closed" || this._phase === "fallback") return;
    if (this.helloTimer !== null) this.timers.clearTimeout(this.helloTimer);
    this.helloTimer = this.timers.setTimeout(() => {
      this.helloTimer = null;
      this.noAnswer();
    }, answerDeadline(this.unanswered, this.random));
  }

  /** An ask went unanswered past its deadline. While the page is hidden it is
   *  asked again without counting (a sleeping tab's timers say nothing about
   *  the server). A channel still opening stops after the ladder's attempts
   *  and falls back, so the host offers a retry instead of opening forever;
   *  a live one keeps asking, its copy and unsent edits kept. */
  private noAnswer(): void {
    if (this._phase === "closed" || this._phase === "fallback") return;
    if (this.visibility?.hidden !== true) this.unanswered += 1;
    if (this._phase === "connecting" && this.unanswered >= LIVE_OPEN_DEADLINE.attempts) {
      this.fallBack(NO_ANSWER);
      return;
    }
    this.ask();
  }

  /** Ask for what the channel waits on: the subscribe again when the server
   *  never answered it (or the copy cannot be read yet), the hello otherwise. */
  private ask(): void {
    if (this.subscribed && this.loro !== null) {
      this.hello();
      return;
    }
    this.release?.();
    this.release = this.socket.subscribe(this.channel);
    this.armDeadline();
  }

  /** The page is shown again: a channel still opening asks now, with a fresh
   *  count, instead of waiting out a deadline the hidden tab stretched. */
  private shownAgain(): void {
    if (this._phase !== "connecting") return;
    this.unanswered = 0;
    this.ask();
  }

  private resync(): void {
    this.clearInflight();
    // One hello at a time: a burst of broadcasts this tab cannot use (after a
    // reload it missed, or history it lacks) asks once now and once after the
    // answer, never once per broadcast — each answer is a whole snapshot.
    if (this.helloTimer !== null) {
      this.resyncAfterSync = true;
      return;
    }
    this.hello();
  }

  /** The tab committed a local change: send it when the lane can. */
  localCommitted(): void {
    this.armIdle();
    this.watchUnsaved();
    this.push();
  }

  /** Whether this copy holds operations the server has not taken. */
  private ahead(): boolean {
    const doc = this._doc;
    if (doc === null) return false;
    if (this.serverVV === null) return true;
    const order = doc.oplogVersion().compare(this.serverVV);
    return order !== 0 && order !== -1;
  }

  /** Start the clock on edits waiting for the server; once they have waited
   *  past the bound the tab says so, until the server has them all. */
  private watchUnsaved(): void {
    if (this.unsavedTimer !== null || this._phase === "closed") return;
    this.unsavedTimer = this.timers.setTimeout(() => {
      this.unsavedTimer = null;
      if (this.ahead()) {
        this.setUnsaved(true);
        this.watchUnsaved();
      } else {
        this.setUnsaved(false);
      }
    }, UNSAVED_AFTER_MS);
  }

  private settled(): void {
    if (this.ahead()) return;
    this.setUnsaved(false);
    // Everything the stash held has been answered for: a later page (or this
    // one, restored from the back-forward cache) must not carry it again.
    if (this.stashed && this.replay === null) this.dropStash();
  }

  private setUnsaved(unsaved: boolean): void {
    if (this._unsaved === unsaved) return;
    this._unsaved = unsaved;
    this.emit("unsaved", unsaved);
  }

  private clearInflight(): void {
    if (this.inflight !== null) this.timers.clearTimeout(this.inflight.timer);
    this.inflight = null;
  }

  private push(): void {
    const doc = this._doc;
    if (doc === null || this.serverVV === null || this._phase !== "live" || !this._canWrite) return;
    if (this.inflight !== null || this.retryTimer !== null) return;
    const local = doc.oplogVersion();
    const ahead = local.compare(this.serverVV);
    if (ahead === 0 || ahead === -1) return;
    const update = doc.export({ mode: "update", from: this.serverVV });
    if (update.length > this.maxUpdateBytes) {
      this.reset("Your last edit was too large to share.", true);
      return;
    }
    const id = this.newId();
    const frames = update.length <= CHUNK_BYTES ? 1 : Math.ceil(update.length / CHUNK_BYTES);
    const wait = this.budget.waitFor(frames, Math.ceil(update.length * 1.4) + 300 * frames);
    if (wait > 0) {
      this.retryTimer = this.timers.setTimeout(() => {
        this.retryTimer = null;
        this.push();
      }, wait);
      return;
    }
    const timer = this.timers.setTimeout(() => {
      // No ack: the update, or its ack, was lost. The next sync says what landed.
      this.inflight = null;
      this.resync();
    }, ACK_TIMEOUT_MS);
    this.inflight = { id, upto: local, timer };
    void this.sendUpdate(id, update, frames);
  }

  private async sendUpdate(id: string, update: Uint8Array, frames: number): Promise<void> {
    this.budget.spend(frames, Math.ceil(update.length * 1.4) + 300 * frames);
    if (update.length <= CHUNK_BYTES) {
      this.socket.send(this.envelope("crdt", { t: "update", update_id: id, data_b64: toBase64(update) }));
      return;
    }
    for (const chunk of await split(update, id)) {
      if (this.inflight?.id !== id) return;
      this.socket.send(this.envelope("crdt", { t: "update", update_id: id, chunk }));
    }
  }

  /** A caret moved: at most one frame per interval, carrying the latest. */
  sendEphemeral(data: Uint8Array): void {
    if (this._phase !== "live" || !this._canWrite) return;
    this.pendingEphemeral = data;
    if (this.ephemeralTimer !== null) return;
    const flush = (): void => {
      this.ephemeralTimer = null;
      const bytes = this.pendingEphemeral;
      if (bytes === null || this._phase !== "live") return;
      const wait = this.budget.waitFor(1, bytes.length * 2 + 300);
      if (wait > 0) {
        this.ephemeralTimer = this.timers.setTimeout(flush, wait);
        return;
      }
      this.pendingEphemeral = null;
      this.budget.spend(1, bytes.length * 2 + 300);
      this.lastEphemeralAt = Date.now();
      this.socket.send(this.envelope("crdt", { t: "ephemeral", data_b64: toBase64(bytes) }));
    };
    const since = Date.now() - this.lastEphemeralAt;
    this.ephemeralTimer = this.timers.setTimeout(flush, Math.max(0, EPHEMERAL_INTERVAL_MS - since));
  }

  private armIdle(): void {
    if (this.idleTimer !== null) this.timers.clearTimeout(this.idleTimer);
    this.idleTimer = this.timers.setTimeout(() => {
      this.idleTimer = null;
      if (this._phase === "live" && this.inflight === null) this.hello();
      this.armIdle();
    }, IDLE_RESYNC_MS);
  }

  // -- inbound -------------------------------------------------------------

  private async onFrame(frame: ServerFrame): Promise<void> {
    if (this._phase === "closed" || this._phase === "fallback") return;
    if (frame.t === "reset") {
      // The server's queue for this socket overflowed and was thrown away:
      // whatever it held for this document is gone. Ask for what is missing
      // now rather than at the next idle check.
      if (this._doc !== null) this.resync();
      return;
    }
    if (frame.t === "subscribed" && frame.channel === this.channel) {
      this.subscribed = true;
      this.setWritable(frame.can_write);
      await this.ensureLoro();
      if (this.loro !== null) this.hello();
      return;
    }
    if (frame.t === "error" && frame.channel === this.channel) {
      // A refusal the server may lift on its own (it was busy, or failed for
      // a moment) is asked again, the tab's copy and its unsent edits kept.
      // Any other refusal is final: waiting on a channel the server will not
      // grant would leave the tab connecting for good.
      if (TRANSIENT_REFUSALS.has(frame.code)) {
        this.resubscribe();
        return;
      }
      this.fallBack(frame.code || "refused");
      return;
    }
    if (frame.t !== "doc" || `doc:${frame.envelope.doc_type}:${frame.envelope.doc_id}` !== this.channel) return;
    const envelope = frame.envelope;
    const payload = envelope.payload;
    switch (envelope.kind) {
      case "snapshot":
        this.savingFrom(payload.saving);
        await this.onSync(envelope.epoch, payload);
        return;
      case "ack":
        // An ack from another epoch settles nothing here: its vector is
        // another document's. The ack timeout's resync settles the edit.
        if (envelope.epoch === this.epoch) this.onAck(payload);
        return;
      case "crdt":
        // Whether saving is paused is about the document, not one epoch of it.
        if (payload.t === "saving") {
          this.savingFrom(payload);
          return;
        }
        // A document is one epoch's history. An update from an epoch this tab
        // has left is dropped; one from an epoch it has not reached yet means
        // it missed the reload, so it asks for the current document. Mixing
        // the two would merge another history's text into this one.
        if (envelope.epoch !== this.epoch) {
          if (envelope.epoch > this.epoch && this._doc !== null && payload.t === "update") this.resync();
          return;
        }
        if (payload.t === "ephemeral") this.onEphemeral(payload);
        else if (payload.t === "gone" && typeof payload.loro_peer === "number") this.emit("gone", payload.loro_peer);
        else if (payload.t === "update") await this.onRemoteUpdate(payload);
        return;
      case "reload":
        this.savingFrom(payload.saving);
        this.resync();
        return;
      case "error":
        this.onError(payload);
        return;
      default:
        return;
    }
  }

  /** The wait before asking again after a busy answer, a rung higher each
   *  time in a row. */
  private nextBusyWait(askedMs: number | null): number {
    const wait = busyWait(this.busyRung, askedMs, this.random);
    this.busyRung += 1;
    return wait;
  }

  private resubscribe(): void {
    if (this.resubscribeTimer !== null || this._phase === "closed" || this._phase === "fallback") return;
    if (this.ahead()) this.watchUnsaved();
    const wait = this.nextBusyWait(null);
    this.resubscribeTimer = this.timers.setTimeout(() => {
      this.resubscribeTimer = null;
      if (this._phase === "closed" || this._phase === "fallback") return;
      this.release?.();
      this.release = this.socket.subscribe(this.channel);
    }, wait);
  }

  private async ensureLoro(): Promise<void> {
    if (this.loro !== null) return;
    try {
      this.loro = await this.loadLoro();
    } catch {
      this.fallBack("load_failed");
    }
  }

  private async onSync(epoch: number, payload: Record<string, unknown>): Promise<void> {
    const loro = this.loro;
    if (loro === null) return;
    let data: Uint8Array | null;
    if (isChunk(payload.chunk)) {
      data = await this.snapshots.add(payload.chunk);
      if (data === null) return;
    } else if (typeof payload.data_b64 === "string") {
      data = fromBase64(payload.data_b64);
    } else {
      return;
    }
    // A reader is never handed a peer: it writes nothing, so any id will do.
    const peer =
      typeof payload.loro_peer === "number" && Number.isSafeInteger(payload.loro_peer) && payload.loro_peer > 0
        ? payload.loro_peer
        : null;
    const limits = payload.limits as { max_text_bytes?: unknown; max_update_bytes?: unknown } | undefined;
    if (typeof limits?.max_text_bytes === "number") this.maxTextBytes = limits.max_text_bytes;
    if (typeof limits?.max_update_bytes === "number") this.maxUpdateBytes = limits.max_update_bytes;
    const serverVV = loro.VersionVector.decode(fromBase64(String(payload.vv_b64 ?? "")));
    if (this._doc === null || epoch !== this.epoch || payload.mode === "snapshot") {
      this.adopt(loro, epoch, peer, data, serverVV);
    } else {
      if (peer !== null && this.loroPeer !== peer) this._doc.setPeerId(BigInt(peer));
      this.loroPeer = peer;
      this.importRemote(data);
      this.serverVV = serverVV;
    }
    if (this.helloTimer !== null) this.timers.clearTimeout(this.helloTimer);
    this.helloTimer = null;
    this.unanswered = 0;
    if (this.resyncAfterSync) {
      this.resyncAfterSync = false;
      this.hello();
    }
    this.clearInflight();
    this.busyRung = 0;
    this.replayStash();
    // An earlier page's edits the server has not answered for go again with
    // every sync (the first, a new socket's, the idle check's): an answer
    // lost on the way is asked for anew, and a new epoch carries them.
    if (this.replay !== null) void this.sendReplay();
    this.setPhase("live");
    this.armIdle();
    this.push();
    this.settled();
  }

  /** A whole document for this tab: a first open, a new epoch, or a reset.
   *  Edits this tab made that the server never acknowledged are carried onto
   *  it — applied as one splice from the text the server last confirmed, and
   *  never deleting what this tab did not see. */
  private adopt(loro: LoroApi, epoch: number, peer: number | null, data: Uint8Array, serverVV: VersionVector): void {
    const previous = this._doc;
    const base = previous !== null ? this.ackedText() : "";
    const local = previous !== null ? this.localText() : "";
    const next = new loro.LoroDoc();
    if (peer !== null) next.setPeerId(BigInt(peer));
    next.import(data);
    const sameDoc = previous !== null && epoch === this.epoch;
    // A type whose content is not one text cannot be carried onto another
    // epoch's document as a splice: what it would lose is offered back.
    const uncarried = previous !== null && !sameDoc && this.textName === null ? this.unacknowledged(next) : "";
    if (sameDoc) {
      // The same epoch sent whole (a vector the server could not use): this
      // copy's own operations are still valid in it — carry them over exactly.
      next.import(previous.export({ mode: "snapshot" }));
    }
    this._doc = next;
    this.epoch = epoch;
    this.loroPeer = peer;
    this.serverVV = serverVV;
    if (!sameDoc && previous !== null && this.textName !== null && carrySplice(next.getText(this.textName), base, local)) {
      next.commit({ origin: "local" });
    }
    this.emit("replaced", next);
    if (uncarried) this.offer({ message: EDITS_NOT_KEPT, restorable: uncarried });
  }

  private importRemote(data: Uint8Array): boolean {
    const doc = this._doc;
    if (doc === null) return false;
    this.emit("beforeRemote");
    let missing = false;
    try {
      const status = doc.import(data);
      missing = status.pending !== null && status.pending.size > 0;
    } catch {
      missing = true;
    }
    this.emit("afterRemote");
    if (missing) this.resync();
    return !missing;
  }

  private async onRemoteUpdate(payload: Record<string, unknown>): Promise<void> {
    let data: Uint8Array | null;
    if (isChunk(payload.chunk)) {
      data = await this.updates.add(payload.chunk);
      if (data === null) return;
    } else if (typeof payload.data_b64 === "string") {
      data = fromBase64(payload.data_b64);
    } else {
      return;
    }
    this.importRemote(data);
  }

  private onAck(payload: Record<string, unknown>): void {
    if (this.settleReplay(payload.update_id, "ack")) return;
    const loro = this.loro;
    if (loro === null || this.inflight === null || payload.update_id !== this.inflight.id) return;
    this.clearInflight();
    this.busyRung = 0;
    if (typeof payload.vv_b64 === "string") {
      this.serverVV = loro.VersionVector.decode(fromBase64(payload.vv_b64));
    }
    this.push();
    this.settled();
  }

  private onEphemeral(payload: Record<string, unknown>): void {
    if (typeof payload.data_b64 !== "string" || typeof payload.loro_peer !== "number") return;
    this.emit("ephemeral", fromBase64(payload.data_b64), {
      loroPeer: payload.loro_peer,
      userId: typeof payload.user_id === "string" ? payload.user_id : "",
      displayName: typeof payload.display_name === "string" ? payload.display_name : "",
      email: typeof payload.email === "string" ? payload.email : "",
    });
  }

  private onError(payload: Record<string, unknown>): void {
    const code = String(payload.code ?? "");
    const named = typeof payload.update_id === "string" ? payload.update_id : null;
    // A refused caret is dropped; the document and the update in flight are
    // untouched by it, whatever the code.
    if (payload.reason === "ephemeral") return;
    const hint = typeof payload.retry_after_ms === "number" ? payload.retry_after_ms : null;
    if (this.settleReplay(named, "error", code, hint)) return;
    const mine = named !== null && this.inflight?.id === named;
    switch (code) {
      case "crdt_resync":
      case "not_synced":
        this.resync();
        return;
      case "stale_epoch":
        // The `reload` that follows names the epoch; the hello it triggers rebases.
        this.clearInflight();
        return;
      case "forbidden":
        this.clearInflight();
        this.setWritable(false);
        return;
      case "crdt_rejected":
        this.reset("Your last edit could not be shared.", true);
        return;
      // The lane is off, or (a file only) the server will not open this file
      // live because it is binary, too large or gone: the tab shows it
      // another way instead of asking again.
      case "crdt_unsupported":
      case "not_editable":
        this.fallBack(code, typeof payload.reason === "string" && payload.reason ? payload.reason : undefined);
        return;
      default: {
        // Busy, a failure on the server's side, a chunk it could not take, or
        // a code this build does not know: none is a verdict on the edits, so
        // they are kept and sent again after a wait that grows while the
        // server keeps answering this way.
        if (!mine && named !== null) return;
        this.clearInflight();
        // A refusal naming no update refused a hello (an open, or a resync
        // of a live tab): ask again. One naming an update: send again.
        const wasHello = named === null;
        if (this.retryTimer === null) {
          const wait = this.nextBusyWait(hint);
          this.retryTimer = this.timers.setTimeout(() => {
            this.retryTimer = null;
            if (wasHello) this.hello();
            else this.push();
          }, wait);
        }
        return;
      }
    }
  }

  /** Throw this tab's copy away and take the server's, offering back the text
   *  only this tab had. A fresh copy writes as a fresh Loro peer. */
  private reset(message: string, offer: boolean): void {
    const lost = offer ? this.unacknowledged(null) : "";
    this.clearInflight();
    this._doc = null;
    this.epoch = 0;
    this.loroPeer = null;
    this.serverVV = null;
    this.offer(lost ? { message, restorable: lost } : { message });
    this.hello();
  }

  private fallBack(reason: string, detail?: string): void {
    if (this._phase === "fallback" || this._phase === "closed") return;
    if (this.helloTimer !== null) this.timers.clearTimeout(this.helloTimer);
    this.helloTimer = null;
    const fallback: LiveFallback = {
      reason,
      ...(detail !== undefined ? { detail } : {}),
      localText: this.localText(),
      ackedText: this.ackedText(),
      unacknowledged: this._canWrite && this.ahead() ? this.unacknowledged(null) : "",
    };
    this.clearInflight();
    this.release?.();
    this.release = null;
    this.setPhase("fallback", fallback);
  }

  private setPhase(phase: LivePhase, fallback?: LiveFallback): void {
    if (this._phase === phase) return;
    this._phase = phase;
    this.emit("phase", phase, fallback);
  }

  /** Take the server's word on whether saving is paused: a `saving` notice,
   *  or the state a sync or a reload carries (`{state, reason}`). Anything
   *  else (a document that rests nowhere, an older server) says nothing. */
  private savingFrom(saving: unknown): void {
    if (typeof saving !== "object" || saving === null) return;
    const { state, reason } = saving as { state?: unknown; reason?: unknown };
    if (state === "paused") this.setSaving({ reason: typeof reason === "string" ? reason : "" });
    else if (state === "ok") this.setSaving(null);
  }

  private setSaving(paused: LiveSavingPaused | null): void {
    if (this._savingPaused?.reason === paused?.reason) return;
    this._savingPaused = paused;
    this.emit("saving", paused);
  }

  private setWritable(canWrite: boolean): void {
    if (this._canWrite === canWrite) return;
    this._canWrite = canWrite;
    this.emit("writable", canWrite);
    if (canWrite) this.push();
  }

  /** How many hellos this channel has sent (diagnostics and tests). */
  get helloCount(): number {
    return this.hellos;
  }
}

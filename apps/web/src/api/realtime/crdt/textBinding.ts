// The Composer's field bound to the `draft` text of a live document.
//
// The binding is the controller between the field (a `TextView`) and the
// channel's LoroDoc:
//
// * A local edit becomes the one splice that turns the old text into the new
//   (the caret disambiguates a run of equal characters, and an emoji is never
//   cut in half), committed as this tab's own change.
// * Somebody else's change: the reader's selection is captured as Loro cursors
//   BEFORE the import and resolved after it, so the caret stays beside the
//   words it was beside — not at a stale offset, and not thrown to the end.
//   While an input method is composing, the field is left alone until the
//   composition ends.
// * Undo and redo are Loro's, and only ever undo this tab's own edits.
// * Carets are Loro cursors in an EphemeralStore, published as this tab's own
//   key and drawn for everybody else, wearing the colour and name the server
//   stamped on them.

import {
  spliceFor,
  splitsPair,
  toCodePointBoundary,
  type BindingNotice,
  type RemoteCaret,
  type TextBinding,
  type TextSelection,
  type TextSplice,
  type TextView,
} from "@alkera/ui";

import { ANONYMOUS_VIEWER_NAME } from "../presence";

import { DEFAULT_TIMERS, type EphemeralStamp, type LiveDocChannel, type Timers } from "./channel";
import type { LoroApi } from "./loro";

type LoroDoc = InstanceType<LoroApi["LoroDoc"]>;
type Cursor = ReturnType<LoroApi["Cursor"]["decode"]>;
type UndoManager = InstanceType<LoroApi["UndoManager"]>;
type EphemeralStore = InstanceType<LoroApi["EphemeralStore"]>;
type Frontiers = ReturnType<LoroDoc["frontiers"]>;

/** How many shown versions are kept: a field is at most a few renders behind. */
const SEEN_VERSIONS = 16;

/** How long a caret outlives its last update: Loro's store drops it then
 *  (it checks every half timeout), which clears the caret of a tab that died
 *  without the server noticing. A tab that leaves cleanly goes at once (the
 *  server's `gone`). */
export const CARET_TIMEOUT_MS = 30_000;
/** How often a tab re-publishes its own caret, so a reader who is still
 *  there but not moving outlives the timeout on everybody else's screen. */
export const CARET_REFRESH_MS = 10_000;
/** What a person with no resolved name is called on their caret: what they
 *  are called on a face too. */
export const ANONYMOUS = ANONYMOUS_VIEWER_NAME;
/** Consecutive keystrokes closer than this undo together. */
export const UNDO_MERGE_MS = 600;
/** How many of a person's own edits undo reaches back through. */
export const UNDO_STEPS = 200;
/** Where the person's edit ends up as the text's own change; nothing else uses it. */
export const LOCAL_ORIGIN = "local";

/**
 * Where one end of the reader's selection is held across somebody else's
 * change: a Loro cursor on a character, and how far past that character the
 * caret sits (0 when held on the character after it, its width when held on
 * the one before).
 */
interface Held {
  cursor: Cursor | undefined;
  past: number;
  /** Held at the very start of the text, which nothing typed can move it off. */
  start?: boolean;
}

interface Captured {
  start: Held;
  end: Held;
  direction: TextSelection["direction"];
}

/**
 * Hold `index` of `shown` against the character on one side of it. A Loro
 * cursor names the character AFTER a position, so text inserted exactly there
 * would push a caret held that way past it: two people typing at one place
 * would interleave letter by letter. A caret ("before") is held on the
 * character before it instead, so what somebody else adds where it stands lands
 * after it and the person keeps typing their own run. A selection holds its
 * start on the character after it and its end on the one before, so text added
 * at either edge stays outside it. A caret at the very start has no character
 * before it and stays at the start.
 */
function hold(text: { getCursor(pos: number): Cursor | undefined }, shown: string, index: number, side: "before" | "after"): Held {
  const at = toCodePointBoundary(shown, index);
  if (side === "before" && at === 0) return { cursor: undefined, past: 0, start: true };
  if (side === "before" && at > 0) {
    const width = at >= 2 && splitsPair(shown, at - 1) ? 2 : 1;
    return { cursor: text.getCursor(at - width), past: width };
  }
  return { cursor: text.getCursor(at), past: 0 };
}

export interface LoroTextBindingOptions {
  channel: LiveDocChannel;
  loro: LoroApi;
  /** A person's colour, from who they are. */
  hueOf: (who: { userId: string; email: string }) => number;
  timers?: Timers;
}

function at(index: number): TextSelection {
  return { start: index, end: index, direction: "none" };
}

/**
 * The splice `edit` (made against `seen`) moved onto `current`, which differs
 * from `seen` by one remote change. An edit before or after the remote change
 * shifts with it; an edit that overlaps it inserts beside it and deletes
 * nothing the person did not see.
 */
/** An offset in a text moved onto the text `gap` turns it into. */
function through(offset: number, gap: TextSplice): number {
  if (offset <= gap.index) return offset;
  if (offset >= gap.index + gap.remove) return offset + gap.insert.length - gap.remove;
  return gap.index + gap.insert.length;
}

export function rebaseSplice(seen: string, current: string, edit: TextSplice): TextSplice | null {
  const remote = spliceFor(seen, current);
  if (remote === null) return edit;
  const shift = remote.insert.length - remote.remove;
  if (edit.index + edit.remove <= remote.index) return edit;
  if (edit.index >= remote.index + remote.remove) return { ...edit, index: edit.index + shift };
  const index = remote.index + remote.insert.length;
  return edit.insert ? { index, remove: 0, insert: edit.insert } : null;
}

export class LoroTextBinding implements TextBinding {
  private readonly channel: LiveDocChannel;
  private readonly loro: LoroApi;
  private readonly hueOf: (who: { userId: string; email: string }) => number;
  private view: TextView | null = null;
  private doc: LoroDoc | null;
  private undoer: UndoManager | null = null;
  private carets: EphemeralStore;
  private stopCarets: (() => void) | null = null;
  private stopLocalCarets: (() => void) | null = null;
  private readonly stamps = new Map<string, EphemeralStamp>();
  /** Carets the server said are gone. Hidden rather than deleted: deleting a
   *  key in the store is this tab's own edit, and it may only write its own.
   *  A key is shown again when its peer publishes again. */
  private readonly hidden = new Set<string>();
  private readonly timers: Timers;
  private refreshTimer: unknown = null;
  private captured: Captured | null = null;
  private deferred = false;
  private selection: TextSelection | null = null;
  /** The versions the field has recently been shown (or produced itself),
   *  newest last, so an edit made on a field that has not caught up yet is
   *  applied to the text the person saw. */
  private seen: { text: string; frontiers: Frontiers }[] = [];
  private readonly caretListeners = new Set<(carets: RemoteCaret[]) => void>();
  private readonly unlisten: () => void;

  constructor(opts: LoroTextBindingOptions) {
    this.channel = opts.channel;
    this.loro = opts.loro;
    this.hueOf = opts.hueOf;
    this.timers = opts.timers ?? DEFAULT_TIMERS;
    this.doc = opts.channel.doc;
    this.carets = this.newCaretStore();
    this.rebind();
    this.armRefresh();
    this.unlisten = this.channel.listen({
      beforeRemote: () => this.capture(),
      afterRemote: () => this.remoteChanged(),
      replaced: (doc) => {
        this.doc = doc;
        this.rebind();
        this.show(this.selection);
        this.publishCaret();
      },
      ephemeral: (data, stamp) => {
        this.stamps.set(String(stamp.loroPeer), stamp);
        this.carets.apply(data);
        this.emitCarets();
      },
      gone: (loroPeer) => {
        this.hidden.add(String(loroPeer));
        this.emitCarets();
      },
    });
  }

  /** The root text this binding edits: a composer draft always has one. */
  private get textName(): string {
    const name = this.channel.textName;
    if (name === null) throw new Error("this document keeps no single text");
    return name;
  }

  get maxBytes(): number {
    return this.channel.limits.maxTextBytes;
  }

  dispose(): void {
    this.unlisten();
    if (this.refreshTimer !== null) this.timers.clearTimeout(this.refreshTimer);
    this.refreshTimer = null;
    this.stopCarets?.();
    this.stopLocalCarets?.();
    this.undoer = null;
    this.view = null;
  }

  // -- the document -------------------------------------------------------

  private rebind(): void {
    const doc = this.doc;
    this.undoer?.free?.();
    this.undoer =
      doc === null ? null : new this.loro.UndoManager(doc, { mergeInterval: UNDO_MERGE_MS, maxUndoSteps: UNDO_STEPS });
    this.stopCarets?.();
    this.stopLocalCarets?.();
    this.carets = this.newCaretStore();
    this.seen = [];
    this.remember();
  }

  private newCaretStore(): EphemeralStore {
    const store = new this.loro.EphemeralStore(CARET_TIMEOUT_MS);
    this.stopCarets = store.subscribe((event) => {
      // A peer that publishes again is back, whatever the server said.
      if (event.by === "import") for (const key of [...event.added, ...event.updated]) this.hidden.delete(key);
      this.emitCarets();
    });
    this.stopLocalCarets = store.subscribeLocalUpdates((bytes: Uint8Array) => this.channel.sendEphemeral(bytes));
    return store;
  }

  text(): string {
    return this.doc?.getText(this.textName).toString() ?? "";
  }

  // -- the field ----------------------------------------------------------

  attach(view: TextView): () => void {
    this.view = view;
    if (view.getText() !== this.text()) view.setText(this.text(), view.getSelection());
    return () => {
      if (this.view === view) this.view = null;
    };
  }

  private show(selection: TextSelection | null): void {
    this.remember();
    this.view?.setText(this.text(), selection);
    this.emitCarets();
  }

  /** Note the document's current text as one the field shows. */
  private remember(): void {
    const doc = this.doc;
    if (doc === null) return;
    this.noteSeen(doc.getText(this.textName).toString(), doc.frontiers());
  }

  private noteSeen(text: string, frontiers: Frontiers): void {
    if (this.seen.at(-1)?.text === text) return;
    this.seen.push({ text, frontiers });
    if (this.seen.length > SEEN_VERSIONS) this.seen.shift();
  }

  edit(before: string, after: string, selection: TextSelection | null): void {
    const doc = this.doc;
    if (doc === null) return;
    const text = doc.getText(this.textName);
    const current = text.toString();
    const splice = spliceFor(before, after, selection?.end);
    if (splice === null) return;
    let caret: number | null;
    if (current === before) {
      if (splice.remove > 0) text.delete(splice.index, splice.remove);
      if (splice.insert) text.insert(splice.index, splice.insert);
      doc.commit({ origin: LOCAL_ORIGIN });
      caret = splice.index + splice.insert.length;
    } else {
      // Somebody else's edit reached the document after the field last showed
      // it: the person's change is made on the text they saw and merged, so
      // both land exactly where each was made.
      caret = this.editSeen(before, splice) ?? this.editRebased(before, current, splice);
      if (caret === null) return;
    }
    this.remember();
    this.channel.localCommitted();
    if (current !== before) {
      if (this.view?.isComposing()) this.deferred = true;
      else this.show(selection === null ? null : at(caret));
    }
    this.select(selection === null || current === before ? selection : at(caret));
  }

  /** `splice` made on the version the field showed as `before`, merged into
   *  the document; where the caret lands, or null when that version is gone. */
  private editSeen(before: string, splice: TextSplice): number | null {
    const doc = this.doc;
    const seen = [...this.seen].reverse().find((v) => v.text === before);
    if (doc === null || seen === undefined) return null;
    const peer = doc.peerIdStr;
    const fork = doc.forkAt(seen.frontiers);
    try {
      // Placed by the characters the person saw, and made here as this tab's
      // own change, so undo takes it back like any other keystroke.
      const local = this.placeSeen(fork, before, splice);
      if (local !== null) return local;
      // The characters it touches moved apart (someone typed inside a range
      // being replaced): made on the version shown and merged instead.
      // The fork writes as this tab, continuing its own counter, which only
      // holds when this tab wrote nothing after that version.
      if (fork.version().get(peer) !== doc.version().get(peer)) return null;
      fork.setPeerId(peer);
      const forked = fork.getText(this.textName);
      if (splice.remove > 0) forked.delete(splice.index, splice.remove);
      if (splice.insert) forked.insert(splice.index, splice.insert);
      fork.commit({ origin: LOCAL_ORIGIN });
      // What the field now shows is this version, which the document never was.
      this.noteSeen(forked.toString(), fork.frontiers());
      // Held on the last character typed, so whatever else lands there stays after it.
      const end = forked.getCursor(splice.index + splice.insert.length, splice.insert ? -1 : 0);
      doc.import(fork.export({ mode: "update", from: doc.oplogVersion() }));
      const placed = end === undefined ? undefined : doc.getCursorPos(end)?.offset;
      return placed ?? doc.getText(this.textName).length;
    } finally {
      fork.free?.();
    }
  }

  /** `splice`, made on `before` (the text of `fork`), applied to the
   *  document at the positions its characters hold there now; null when they
   *  no longer sit as they did (a deleted neighbour, a range split apart). */
  private placeSeen(fork: LoroDoc, before: string, splice: TextSplice): number | null {
    const doc = this.doc;
    if (doc === null) return null;
    const text = doc.getText(this.textName);
    const current = text.toString();
    const shown = fork.getText(this.textName);
    // Where the character at `index` of what was shown is now, if it is still there.
    const now = (index: number): number | null => {
      const cursor = shown.getCursor(index);
      const offset = cursor === undefined ? undefined : doc.getCursorPos(cursor)?.offset;
      if (offset === undefined || current[offset] !== before[index]) return null;
      return offset;
    };
    let at: number;
    if (splice.remove > 0) {
      const first = now(splice.index);
      const last = now(splice.index + splice.remove - 1);
      if (first === null || last === null || last - first !== splice.remove - 1) return null;
      text.delete(first, splice.remove);
      at = first;
    } else if (splice.index === 0) {
      at = 0;
    } else {
      const previous = now(splice.index - 1);
      if (previous === null) return null;
      at = previous + 1;
    }
    if (splice.insert) text.insert(at, splice.insert);
    doc.commit({ origin: LOCAL_ORIGIN });
    return at + splice.insert.length;
  }

  /** The fallback when the shown version is unknown: the splice moved through
   *  the difference between what was shown and what the document holds. */
  private editRebased(before: string, current: string, splice: TextSplice): number | null {
    const doc = this.doc;
    const moved = rebaseSplice(before, current, splice);
    if (doc === null || moved === null) return null;
    const text = doc.getText(this.textName);
    if (moved.remove > 0) text.delete(moved.index, moved.remove);
    if (moved.insert) text.insert(moved.index, moved.insert);
    doc.commit({ origin: LOCAL_ORIGIN });
    return moved.index + moved.insert.length;
  }

  select(selection: TextSelection | null): void {
    this.selection = selection;
    this.publishCaret();
  }

  private publishCaret(): void {
    const doc = this.doc;
    const peer = this.channel.peer;
    if (doc === null || peer === null) return;
    const key = String(peer);
    const selection = this.selection;
    if (selection === null) {
      this.carets.delete(key);
      return;
    }
    const text = doc.getText(this.textName);
    const shown = text.toString();
    const head = selection.direction === "backward" ? selection.start : selection.end;
    const tail = selection.direction === "backward" ? selection.end : selection.start;
    const focus = text.getCursor(toCodePointBoundary(shown, head));
    const anchor = text.getCursor(toCodePointBoundary(shown, tail));
    if (focus === undefined || anchor === undefined) return;
    this.carets.set(key, { anchor: anchor.encode(), focus: focus.encode() });
  }

  compositionEnded(): void {
    if (!this.deferred) return;
    this.deferred = false;
    this.show(this.view?.getSelection() ?? null);
  }

  undo(): boolean {
    return this.history(true);
  }

  redo(): boolean {
    return this.history(false);
  }

  private history(undo: boolean): boolean {
    const undoer = this.undoer;
    if (undoer === null) return false;
    const before = this.text();
    const moved = undo ? undoer.undo() : undoer.redo();
    if (!moved) return false;
    const after = this.text();
    this.channel.localCommitted();
    // The caret goes where the undone (or redone) change was.
    const splice = spliceFor(before, after);
    const at = splice === null ? after.length : splice.index + splice.insert.length;
    const selection: TextSelection = { start: at, end: at, direction: "none" };
    this.view?.setText(after, selection);
    this.select(selection);
    this.emitCarets();
    return true;
  }

  // -- somebody else's change ---------------------------------------------

  private capture(): void {
    const doc = this.doc;
    const selection = this.view?.getSelection() ?? null;
    if (doc === null || selection === null) {
      this.captured = null;
      return;
    }
    // The field's selection counts in the text the field shows, which trails
    // the document while a render is pending: it is held on that version, and
    // the cursors (character ids) then resolve in the document as it is now.
    let text = doc.getText(this.textName);
    let shown = text.toString();
    const drawn = this.view?.getText();
    const seen = drawn === undefined || drawn === shown ? undefined : [...this.seen].reverse().find((v) => v.text === drawn);
    const fork = seen === undefined ? null : doc.forkAt(seen.frontiers);
    let start = selection.start;
    let end = selection.end;
    if (fork !== null && drawn !== undefined) {
      text = fork.getText(this.textName);
      shown = drawn;
    } else if (drawn !== undefined && drawn !== shown) {
      // A text the document never held (this tab's own edit, made on a field
      // that had not drawn somebody else's yet): the offsets are moved
      // through the difference to the document.
      const gap = spliceFor(drawn, shown);
      if (gap !== null) {
        start = through(start, gap);
        end = through(end, gap);
      }
    }
    try {
      const caret = start === end;
      this.captured = {
        start: hold(text, shown, start, caret ? "before" : "after"),
        end: hold(text, shown, end, "before"),
        direction: selection.direction,
      };
    } finally {
      fork?.free?.();
    }
  }

  private resolve(cursor: Cursor | undefined, fallback: number): number {
    const doc = this.doc;
    if (doc === null || cursor === undefined) return fallback;
    try {
      return doc.getCursorPos(cursor)?.offset ?? fallback;
    } catch {
      return fallback;
    }
  }

  private place(held: Held, fallback: number): number {
    if (held.start) return 0;
    if (held.cursor === undefined) return fallback;
    const offset = this.resolve(held.cursor, -1);
    return offset < 0 ? fallback : offset + held.past;
  }

  private remoteChanged(): void {
    const captured = this.captured;
    this.captured = null;
    if (this.view === null) {
      this.emitCarets();
      return;
    }
    if (this.view.isComposing()) {
      // Rewriting the field mid-composition would break the input method; the
      // document has the change and the field catches up when it ends.
      this.deferred = true;
      this.emitCarets();
      return;
    }
    const text = this.text();
    let selection: TextSelection | null = null;
    if (captured !== null) {
      const start = Math.min(this.place(captured.start, text.length), text.length);
      const end = Math.min(this.place(captured.end, start), text.length);
      selection = { start: Math.min(start, end), end: Math.max(start, end), direction: captured.direction };
    }
    this.show(selection);
    if (selection !== null) this.select(selection);
  }

  // -- carets ---------------------------------------------------------------

  private armRefresh(): void {
    this.refreshTimer = this.timers.setTimeout(() => {
      if (this.selection !== null) this.publishCaret();
      this.armRefresh();
    }, CARET_REFRESH_MS);
  }

  private emitCarets(): void {
    const carets = this.remoteCarets();
    this.caretListeners.forEach((l) => l(carets));
  }

  private remoteCarets(): RemoteCaret[] {
    const doc = this.doc;
    if (doc === null) return [];
    const mine = this.channel.peer === null ? null : String(this.channel.peer);
    const states = this.carets.getAllStates() as Record<string, { anchor?: Uint8Array; focus?: Uint8Array } | undefined>;
    const out: RemoteCaret[] = [];
    for (const [key, value] of Object.entries(states)) {
      if (key === mine || !value?.focus || this.hidden.has(key)) continue;
      const stamp = this.stamps.get(key);
      if (stamp === undefined) continue;
      let focus: number;
      let anchor: number;
      try {
        focus = this.resolve(this.loro.Cursor.decode(value.focus), -1);
        anchor = value.anchor ? this.resolve(this.loro.Cursor.decode(value.anchor), focus) : focus;
      } catch {
        continue;
      }
      if (focus < 0) continue;
      out.push({
        id: key,
        name: stamp.displayName.trim() || ANONYMOUS,
        hue: this.hueOf({ userId: stamp.userId, email: stamp.email }),
        offset: focus,
        anchor,
      });
    }
    return out;
  }

  subscribeCarets(listener: (carets: RemoteCaret[]) => void): () => void {
    this.caretListeners.add(listener);
    listener(this.remoteCarets());
    return () => void this.caretListeners.delete(listener);
  }

  /** Straight onto the channel, never through a relay of this binding's own:
   *  the channel holds an offer of text until somebody listens for one, and a
   *  relay with no subscriber would take the offer and show it to nobody. */
  subscribeNotice(listener: (notice: BindingNotice | null) => void): () => void {
    return this.channel.listen({ notice: listener });
  }
}

// A CodeMirror 6 editor bound to the content text of a live document.
//
// The binding is the controller between the editor view and the channel's
// LoroDoc, and the CodeMirror counterpart of the composer's `LoroTextBinding`:
//
// * A local transaction becomes the same edits in Loro, committed as this
//   tab's own change and handed to the channel to send.
// * Somebody else's change (an import) becomes a CodeMirror transaction built
//   from Loro's delta, so the editor maps the reader's selection through it:
//   text typed exactly where the caret stands lands after it, and text typed
//   elsewhere moves it by exactly as much as it should.
// * Offsets are UTF-16 on both sides, and line breaks map exactly
//   (`lineBreaks.ts`): the editor's text is always the document's, byte for
//   byte, CRLF included. Should the two ever disagree (an edit the editor could
//   not express, half of a `\r\n` deleted elsewhere), the editor is brought
//   back to the document with the smallest change that does it.
// * Undo and redo are Loro's UndoManager: a person undoes only their own edits.
// * Carets are Loro cursors in an EphemeralStore under this tab's Loro peer,
//   and everybody else's carry the name and colour the server stamped on
//   them. The caller's `CaretLayer` draws them; the binding builds no DOM.
// * A reader's editor is read-only; the server's word on writing moves it.
//
// It imports CodeMirror, so it is loaded only with the editor, never up front.

import {
  Annotation,
  Compartment,
  EditorSelection,
  EditorState,
  type ChangeSpec,
  type Extension,
  type StateEffect,
  type Transaction,
} from "@codemirror/state";
import {
  EditorView,
  ViewPlugin,
  keymap,
  type KeyBinding,
  type ViewUpdate,
} from "@codemirror/view";
import { spliceFor } from "@alkera/ui";

import { DEFAULT_TIMERS, type EphemeralStamp, type LiveDocChannel, type Timers } from "./channel";
import { fromLoro, lineSeparatorFor, toLoro, type LineSeparator } from "./lineBreaks";
import type { LoroApi } from "./loro";
import { ANONYMOUS, CARET_REFRESH_MS, CARET_TIMEOUT_MS, LOCAL_ORIGIN, UNDO_MERGE_MS, UNDO_STEPS } from "./textBinding";

type LoroDoc = InstanceType<LoroApi["LoroDoc"]>;
type LoroText = InstanceType<LoroApi["LoroText"]>;
type UndoManager = InstanceType<LoroApi["UndoManager"]>;
type EphemeralStore = InstanceType<LoroApi["EphemeralStore"]>;

/** Marks a transaction that carries somebody else's change into the editor. */
export const fromDocument = Annotation.define<boolean>();

/** A remote caret, in editor positions. */
export interface EditorCaret {
  id: string;
  name: string;
  hue: number;
  head: number;
  anchor: number;
  /** How many times this caret has moved: a move draws its name again. */
  moves: number;
}

/** How the editor draws everybody else's carets: an extension holding them,
 *  and the effect that replaces them. The drawing is the page's (it builds
 *  DOM); the binding only says where each caret stands. */
export interface CaretLayer {
  extension: Extension;
  draw: (carets: readonly EditorCaret[]) => StateEffect<unknown>;
}

/** Whether a reader may type: never a caret or an insertion for one who may
 *  not, rather than keystrokes that vanish. */
function writing(canWrite: boolean): Extension {
  return [EditorState.readOnly.of(!canWrite), EditorView.editable.of(canWrite)];
}

/** Undo and redo kept by somebody else: a notebook keeps one history for all
 *  its cells, so undo in any cell takes back this person's last change. */
export interface ExternalHistory {
  undo(): boolean;
  redo(): boolean;
}

/** Where the editor's caret stands, as Loro cursors in the bound text. */
export interface CaretCursors {
  anchor: Uint8Array;
  focus: Uint8Array;
}

export interface CodeMirrorBindingOptions {
  channel: LiveDocChannel;
  loro: LoroApi;
  /** The text the editor is bound to. Unset: the document type's root text.
   *  A container path (a notebook cell's source) is resolved afresh on every
   *  change, so a cell whose text is replaced (its kind changed) rebinds. */
  text?: (doc: LoroDoc) => LoroText | undefined;
  /** Undo and redo from a history the caller keeps. Unset: the binding keeps
   *  its own, of this tab's edits only. */
  history?: ExternalHistory;
  /** The caller publishes and draws carets for several editors on one
   *  document (a notebook): the binding keeps no caret store of its own and
   *  only converts between the editor's selection and Loro cursors. */
  sharedCarets?: boolean;
  /** Told whenever the editor's selection or focus changes, so a caller
   *  keeping shared carets can publish. */
  onSelection?: () => void;
  /** A person's colour, from who they are. */
  hueOf: (who: { userId: string; email: string }) => number;
  /** Draws everybody else's carets; without one they are tracked, not drawn. */
  carets?: CaretLayer;
  timers?: Timers;
}

interface Pending {
  from: number;
  to: number;
  insert: string;
}

export class LoroCodeMirrorBinding {
  private readonly channel: LiveDocChannel;
  private readonly loro: LoroApi;
  private readonly hueOf: CodeMirrorBindingOptions["hueOf"];
  private readonly caretLayer: CaretLayer | undefined;
  private readonly timers: Timers;
  private readonly textOf: (doc: LoroDoc) => LoroText | undefined;
  private readonly externalHistory: ExternalHistory | undefined;
  private readonly sharedCarets: boolean;
  private readonly onSelection: (() => void) | undefined;
  /** The container the editor last showed, so a swapped one is noticed. */
  private boundId: string | null = null;
  private doc: LoroDoc | null;
  private undoer: UndoManager | null = null;
  private readonly moves = new Map<string, number>();
  private readonly lastWhere = new Map<string, string>();
  private carets: EphemeralStore;
  private readonly stamps = new Map<string, EphemeralStamp>();
  private readonly hidden = new Set<string>();
  private stopDoc: (() => void) | null = null;
  private stopCarets: (() => void) | null = null;
  private stopLocalCarets: (() => void) | null = null;
  private readonly unlisten: () => void;
  private refreshTimer: unknown = null;
  private view: EditorView | null = null;
  /** Set while this binding writes the editor's own edit into Loro, so the
   *  document's event for it is not carried back into the editor. */
  private writing = false;
  private readonly readOnly = new Compartment();
  /** How the editor splits lines; fixed when its state is made. */
  readonly separator: LineSeparator;
  /** Everything the editor needs from the binding. */
  readonly extension: Extension;

  constructor(opts: CodeMirrorBindingOptions) {
    this.channel = opts.channel;
    this.loro = opts.loro;
    this.hueOf = opts.hueOf;
    this.caretLayer = opts.carets;
    this.timers = opts.timers ?? DEFAULT_TIMERS;
    const rootName = opts.channel.textName;
    this.textOf = opts.text ?? ((doc) => (rootName === null ? undefined : doc.getText(rootName)));
    this.externalHistory = opts.history;
    this.sharedCarets = opts.sharedCarets === true;
    this.onSelection = opts.onSelection;
    this.doc = opts.channel.doc;
    this.separator = lineSeparatorFor(this.text());
    this.carets = this.newCaretStore();
    this.rebind();
    if (!this.sharedCarets) this.armRefresh();
    this.extension = [
      EditorState.lineSeparator.of(this.separator),
      this.readOnly.of(writing(this.channel.canWrite)),
      this.caretLayer?.extension ?? [],
      ViewPlugin.define((view) => this.plug(view)),
    ];
    this.unlisten = this.channel.listen({
      replaced: (doc) => {
        this.doc = doc;
        this.rebind();
        this.syncFromDocument();
        this.publishCaret();
      },
      ephemeral: (data, stamp) => {
        if (this.sharedCarets) return;
        this.stamps.set(String(stamp.loroPeer), stamp);
        this.carets.apply(data);
        this.drawCarets();
      },
      gone: (loroPeer) => {
        if (this.sharedCarets) return;
        this.hidden.add(String(loroPeer));
        this.drawCarets();
      },
      writable: (canWrite) => {
        this.view?.dispatch({ effects: this.readOnly.reconfigure(writing(canWrite)) });
      },
    });
  }

  /** The plugin instance the editor runs: it hands every update here. */
  private plug(view: EditorView): { update(update: ViewUpdate): void; destroy(): void } {
    this.view = view;
    return {
      update: (update) => this.onUpdate(update),
      destroy: () => {
        if (this.view === view) this.view = null;
      },
    };
  }

  /** The document's content now. */
  text(): string {
    return this.bound()?.toString() ?? "";
  }

  /** The text the editor is bound to now, if the document holds it. */
  private bound(): LoroText | undefined {
    const doc = this.doc;
    if (doc === null) return undefined;
    try {
      return this.textOf(doc);
    } catch {
      return undefined;
    }
  }

  /** The state an editor starts from: the document's text, this binding's
   *  extension, and whatever else the editor brings. */
  createState(extensions: Extension = []): EditorState {
    return EditorState.create({ doc: this.text(), extensions: [this.extension, extensions] });
  }

  /** Undo, redo and the other keys the binding owns, for the editor's keymap
   *  (ahead of the default one; the editor brings no history of its own). */
  get keymap(): KeyBinding[] {
    return [
      { key: "Mod-z", run: () => this.undo(), preventDefault: true },
      { key: "Mod-y", run: () => this.redo(), preventDefault: true },
      { key: "Mod-Shift-z", run: () => this.redo(), preventDefault: true },
    ];
  }

  /** The keymap above as an extension, at the precedence it needs. */
  keys(): Extension {
    return keymap.of(this.keymap);
  }

  dispose(): void {
    this.unlisten();
    if (this.refreshTimer !== null) this.timers.clearTimeout(this.refreshTimer);
    this.refreshTimer = null;
    this.stopDoc?.();
    this.stopCarets?.();
    this.stopLocalCarets?.();
    this.undoer = null;
    this.view = null;
  }

  // -- the document ---------------------------------------------------------

  private rebind(): void {
    const doc = this.doc;
    this.stopDoc?.();
    this.stopDoc = null;
    this.undoer?.free?.();
    this.undoer =
      doc === null || this.externalHistory !== undefined
        ? null
        : new this.loro.UndoManager(doc, { mergeInterval: UNDO_MERGE_MS, maxUndoSteps: UNDO_STEPS });
    this.boundId = this.bound()?.id ?? null;
    this.stopCarets?.();
    this.stopLocalCarets?.();
    this.carets = this.newCaretStore();
    if (doc !== null) {
      this.stopDoc = doc.subscribe((event) => {
        if (this.writing) return;
        const target = this.bound()?.id ?? null;
        if (target !== this.boundId) {
          // The text itself was replaced (a cell's kind changed, or the cell
          // went): the editor shows whatever the document now holds.
          this.boundId = target;
          this.syncFromDocument();
          this.drawCarets();
          return;
        }
        for (const change of event.events) {
          if (change.target !== target || change.diff.type !== "text") continue;
          this.applyDelta(change.diff.diff as DeltaItem[]);
        }
        this.drawCarets();
      });
    }
  }

  private newCaretStore(): EphemeralStore {
    const store = new this.loro.EphemeralStore(CARET_TIMEOUT_MS);
    this.stopCarets = store.subscribe((event) => {
      // This tab's own caret is never drawn, and it changes while the editor
      // is mid-update: only somebody else's redraws.
      if (event.by === "local") return;
      if (event.by === "import") for (const key of [...event.added, ...event.updated]) this.hidden.delete(key);
      this.drawCarets();
    });
    this.stopLocalCarets = store.subscribeLocalUpdates((bytes: Uint8Array) => this.channel.sendEphemeral(bytes));
    return store;
  }

  // -- the editor's own edits ---------------------------------------------

  private onUpdate(update: ViewUpdate): void {
    for (const tr of update.transactions) {
      if (tr.docChanged && tr.annotation(fromDocument) !== true) this.write(tr);
    }
    if (update.docChanged || update.selectionSet || update.focusChanged) {
      this.publishCaret();
      this.onSelection?.();
    }
  }

  /** The editor's transaction, made in Loro as this tab's own change. */
  private write(tr: Transaction): void {
    const doc = this.doc;
    if (doc === null) return;
    const before = tr.startState.doc;
    const edits: Pending[] = [];
    tr.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
      edits.push({
        from: toLoro(before, fromA, this.separator),
        to: toLoro(before, toA, this.separator),
        insert: inserted.sliceString(0, inserted.length, this.separator),
      });
    });
    const text = this.bound();
    if (text === undefined) {
      queueMicrotask(() => this.syncFromDocument());
      return;
    }
    this.writing = true;
    try {
      // Back to front, so each edit's offsets are still the ones it was made at.
      for (const edit of edits.reverse()) {
        if (edit.to > edit.from) text.delete(edit.from, edit.to - edit.from);
        if (edit.insert) text.insert(edit.from, edit.insert);
      }
      doc.commit({ origin: LOCAL_ORIGIN });
    } catch {
      // An edit Loro will not take as given (one splitting a surrogate pair):
      // the editor goes back to what the document holds, once the update that
      // carried the edit has finished.
      this.writing = false;
      queueMicrotask(() => this.syncFromDocument());
      return;
    } finally {
      this.writing = false;
    }
    this.channel.localCommitted();
  }

  // -- somebody else's change ---------------------------------------------

  /** Loro's delta for an import, carried into the editor as one transaction. */
  private applyDelta(delta: readonly DeltaItem[]): void {
    const view = this.view;
    if (view === null) return;
    const before = view.state.doc;
    const changes: ChangeSpec[] = [];
    let index = 0;
    for (const item of delta) {
      if ("retain" in item) {
        index += item.retain;
        continue;
      }
      const from = fromLoro(before, index, this.separator);
      if (from === null) return this.syncFromDocument();
      if ("delete" in item) {
        const to = fromLoro(before, index + item.delete, this.separator);
        if (to === null) return this.syncFromDocument();
        changes.push({ from, to });
        index += item.delete;
      } else if ("insert" in item && typeof item.insert === "string") {
        changes.push({ from, insert: item.insert });
      }
    }
    if (changes.length === 0) return;
    try {
      view.dispatch({ changes, annotations: [fromDocument.of(true)] });
    } catch {
      return this.syncFromDocument();
    }
    if (view.state.sliceDoc() !== this.text()) this.syncFromDocument();
  }

  /** Bring the editor to exactly the document's text with the smallest change
   *  that does it, so the reader's place survives wherever it can. */
  private syncFromDocument(): void {
    const view = this.view;
    if (view === null) return;
    const shown = view.state.sliceDoc();
    const target = this.text();
    const splice = spliceFor(shown, target);
    if (splice === null) return;
    const before = view.state.doc;
    const from = fromLoro(before, splice.index, this.separator) ?? 0;
    const to = fromLoro(before, splice.index + splice.remove, this.separator) ?? before.length;
    view.dispatch({ changes: { from, to, insert: splice.insert }, annotations: [fromDocument.of(true)] });
    if (view.state.sliceDoc() !== target) {
      // The separator cannot express it in place: replace the whole text.
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: target },
        annotations: [fromDocument.of(true)],
      });
    }
  }

  // -- undo ---------------------------------------------------------------

  undo(): boolean {
    return this.history(true);
  }

  redo(): boolean {
    return this.history(false);
  }

  private history(undo: boolean): boolean {
    const undoer = this.externalHistory ?? this.undoer;
    const view = this.view;
    if (undoer === null || view === null || !this.channel.canWrite) return false;
    const before = view.state.sliceDoc();
    // The undone change arrives as a local event, carried into the editor
    // like any change the editor did not make.
    const moved = undo ? undoer.undo() : undoer.redo();
    // A shared history may have undone a change in another editor: that one
    // follows from its own subscription, and this one has nothing to move.
    if (this.externalHistory !== undefined) return true;
    if (!moved) return true;
    this.syncFromDocument();
    this.channel.localCommitted();
    // The caret goes where the undone (or redone) change was.
    const splice = spliceFor(before, view.state.sliceDoc());
    if (splice !== null) {
      const at = fromLoro(view.state.doc, splice.index + splice.insert.length, this.separator);
      if (at !== null) view.dispatch({ selection: EditorSelection.cursor(at), scrollIntoView: true });
    }
    return true;
  }

  // -- carets ---------------------------------------------------------------

  /** The editor's selection as cursors in the bound text, for a caller that
   *  publishes carets itself; null when the editor is not focused. */
  selectionCursors(): CaretCursors | null {
    const view = this.view;
    const text = this.bound();
    if (view === null || text === undefined || !view.hasFocus) return null;
    const range = view.state.selection.main;
    const focus = text.getCursor(toLoro(view.state.doc, range.head, this.separator));
    const anchor = text.getCursor(toLoro(view.state.doc, range.anchor, this.separator));
    if (focus === undefined || anchor === undefined) return null;
    return { anchor: anchor.encode(), focus: focus.encode() };
  }

  /** Where an encoded cursor stands in this editor, or null when it names
   *  another text (another cell) or nothing that is still there. */
  editorPosition(encoded: Uint8Array): number | null {
    const doc = this.doc;
    const id = this.bound()?.id;
    if (doc === null || id === undefined) return null;
    try {
      const cursor = this.loro.Cursor.decode(encoded);
      if (cursor.containerId() !== id) return null;
    } catch {
      return null;
    }
    return this.resolve(doc, encoded);
  }

  /** Draw carets the caller resolved (shared-caret mode). */
  showCarets(carets: readonly EditorCaret[]): void {
    const view = this.view;
    if (view === null || this.caretLayer === undefined) return;
    view.dispatch({ effects: this.caretLayer.draw(carets) });
  }

  private armRefresh(): void {
    this.refreshTimer = this.timers.setTimeout(() => {
      this.publishCaret();
      this.armRefresh();
    }, CARET_REFRESH_MS);
  }

  private publishCaret(): void {
    const doc = this.doc;
    const view = this.view;
    const peer = this.channel.peer;
    if (doc === null || view === null || peer === null || !this.channel.canWrite || this.sharedCarets) return;
    const key = String(peer);
    if (!view.hasFocus) {
      if (this.carets.get(key) !== undefined) this.carets.delete(key);
      return;
    }
    const range = view.state.selection.main;
    const text = this.bound();
    if (text === undefined) return;
    const focus = text.getCursor(toLoro(view.state.doc, range.head, this.separator));
    const anchor = text.getCursor(toLoro(view.state.doc, range.anchor, this.separator));
    if (focus === undefined || anchor === undefined) return;
    this.carets.set(key, { anchor: anchor.encode(), focus: focus.encode() });
  }

  /** Everybody else's carets, in editor positions. */
  remoteCarets(): EditorCaret[] {
    const doc = this.doc;
    const view = this.view;
    if (doc === null || view === null) return [];
    const mine = this.channel.peer === null ? null : String(this.channel.peer);
    const states = this.carets.getAllStates() as Record<string, { anchor?: Uint8Array; focus?: Uint8Array } | undefined>;
    const out: EditorCaret[] = [];
    for (const [key, value] of Object.entries(states)) {
      if (key === mine || !value?.focus || this.hidden.has(key)) continue;
      const stamp = this.stamps.get(key);
      if (stamp === undefined) continue;
      const head = this.resolve(doc, value.focus);
      if (head === null) continue;
      const anchor = value.anchor ? (this.resolve(doc, value.anchor) ?? head) : head;
      const where = `${value.focus.join(",")}|${value.anchor?.join(",") ?? ""}`;
      if (this.lastWhere.get(key) !== where) {
        this.lastWhere.set(key, where);
        this.moves.set(key, (this.moves.get(key) ?? 0) + 1);
      }
      out.push({
        id: key,
        name: stamp.displayName.trim() || ANONYMOUS,
        hue: this.hueOf({ userId: stamp.userId, email: stamp.email }),
        head,
        anchor,
        moves: this.moves.get(key) ?? 0,
      });
    }
    return out;
  }

  private resolve(doc: LoroDoc, encoded: Uint8Array): number | null {
    const view = this.view;
    if (view === null) return null;
    try {
      const offset = doc.getCursorPos(this.loro.Cursor.decode(encoded))?.offset;
      return offset === undefined ? null : fromLoro(view.state.doc, offset, this.separator);
    } catch {
      return null;
    }
  }

  private drawCarets(): void {
    const view = this.view;
    if (view === null || this.sharedCarets) return;
    if (this.caretLayer === undefined) return;
    view.dispatch({ effects: this.caretLayer.draw(this.remoteCarets()) });
  }
}

type DeltaItem = { retain: number } | { delete: number } | { insert: unknown };

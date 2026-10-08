// A live text binding: what lets a Composer's field be one view of a shared,
// conflict-free document instead of a value that is copied in and out.
//
// The binding is the controller and the field is the view. The field tells the
// binding what the person did (an edit, a caret move, an IME composition ending,
// an undo); the binding tells the field what the text is now and where the
// selection belongs after somebody else's edit. Nothing here knows what the
// document is made of — the portal implements it over a Loro text, and a host
// with no shared document passes no binding at all.
//
// Offsets are JavaScript string indices (UTF-16 code units), the unit a
// textarea reports its selection in.

import type { RemoteCaret } from "./Composer";

/** A selection in the field: `start <= end`; `direction` says which end is the head. */
export interface TextSelection {
  start: number;
  end: number;
  direction: "forward" | "backward" | "none";
}

/** What the binding may ask of the field. */
export interface TextView {
  /** The text on screen. */
  getText(): string;
  /** The selection, or null when the field does not have focus (nothing to keep). */
  getSelection(): TextSelection | null;
  /** Show `text`, and put the selection at `selection` when the field has focus. */
  setText(text: string, selection: TextSelection | null): void;
  /** Whether an input method is composing: a remote edit waits until it ends. */
  isComposing(): boolean;
}

/** Something the person should be told about their text, with what to offer. */
export interface BindingNotice {
  message: string;
  /** Text that could not be shared and is offered back to them. */
  restorable?: string;
}

export interface TextBinding {
  /** The text the shared document holds now. */
  text(): string;
  /** Start driving `view`; the returned function stops. */
  attach(view: TextView): () => void;
  /** The person changed the text from `before` to `after`; `selection` is
   *  where the caret ended up (it disambiguates repeated characters). */
  edit(before: string, after: string, selection: TextSelection | null): void;
  /** The person's caret moved. */
  select(selection: TextSelection | null): void;
  /** An IME composition ended: remote edits that waited for it apply now. */
  compositionEnded(): void;
  /** Undo / redo this person's own edits; `false` when there was nothing to undo. */
  undo(): boolean;
  redo(): boolean;
  /** The other people's carets in this text. */
  subscribeCarets(listener: (carets: RemoteCaret[]) => void): () => void;
  /** What the person should be told, or null when nothing. */
  subscribeNotice(listener: (notice: BindingNotice | null) => void): () => void;
  /** The most UTF-8 bytes the text may hold (0: no cap). */
  readonly maxBytes: number;
}

/** One contiguous change: delete `remove` code units at `index`, then insert `insert`. */
export interface TextSplice {
  index: number;
  remove: number;
  insert: string;
}

const isHigh = (code: number): boolean => code >= 0xd800 && code <= 0xdbff;
const isLow = (code: number): boolean => code >= 0xdc00 && code <= 0xdfff;

/** Whether `index` falls between the two halves of a surrogate pair in `text`. */
export function splitsPair(text: string, index: number): boolean {
  return (
    index > 0 &&
    index < text.length &&
    isHigh(text.charCodeAt(index - 1)) &&
    isLow(text.charCodeAt(index))
  );
}

/** `index` moved back off the middle of a surrogate pair, clamped into `text`. */
export function toCodePointBoundary(text: string, index: number): number {
  const clamped = Math.max(0, Math.min(index, text.length));
  return splitsPair(text, clamped) ? clamped - 1 : clamped;
}

/**
 * The one splice that turns `before` into `after`.
 *
 * The longest common prefix and suffix fence the change in. When a run of equal
 * characters makes the fence ambiguous — typing "a" into "aa" could be an insert
 * at 0, 1 or 2 — `caret` (where the selection ended after the edit) picks the
 * reading the person actually performed, so their edit merges where they made
 * it. The splice never starts or ends inside a surrogate pair, so an emoji is
 * always inserted or removed whole. `null` when nothing changed.
 */
export function spliceFor(before: string, after: string, caret?: number): TextSplice | null {
  if (before === after) return null;
  const max = Math.min(before.length, after.length);
  let prefix = 0;
  while (prefix < max && before.charCodeAt(prefix) === after.charCodeAt(prefix)) prefix += 1;
  let suffix = 0;
  while (
    suffix < max - prefix &&
    before.charCodeAt(before.length - 1 - suffix) === after.charCodeAt(after.length - 1 - suffix)
  ) {
    suffix += 1;
  }
  // The caret sits at the end of what was typed. When the fence above put the
  // change later than the caret, the equal characters before the caret are
  // the ones that moved: slide the change left by that much.
  if (caret !== undefined) {
    const end = after.length - suffix;
    const slide = Math.max(0, Math.min(end - caret, prefix));
    prefix -= slide;
    suffix += slide;
  }
  // Never cut through a surrogate pair at either edge.
  if (splitsPair(before, prefix) || splitsPair(after, prefix)) prefix -= 1;
  while (
    suffix > 0 &&
    (splitsPair(before, before.length - suffix) || splitsPair(after, after.length - suffix))
  ) {
    suffix -= 1;
  }
  return {
    index: prefix,
    remove: before.length - prefix - suffix,
    insert: after.slice(prefix, after.length - suffix),
  };
}

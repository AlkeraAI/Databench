// An interrupt card's keys reach it wherever the reader's focus is. The ask is
// the one thing on screen waiting on them, so Enter and Escape answer it
// without asking them to click the card first -- the press that was already on
// their fingers is the press that works.
//
// The keys are only borrowed, never taken. They stand down wherever Enter and
// Escape already mean something to the reader: inside a field they are typing
// in, on a control they have tabbed to (Enter is that control's activation),
// under a modifier that re-means them, mid-composition in an IME, with another
// menu or dialog in front, and inside the card itself (a focused button
// activates natively, the always-allow menu roves on its own) -- so the
// document listener never answers a press that already belonged to something
// else, and never swallows one.

import { useEffect, useRef } from "react";

/** What the front card does with a key that reached it from outside its DOM. */
export interface CardKeys {
  /** Enter. Absent where the card offers no approval, and then Enter does
   *  nothing rather than falling through to a different answer. */
  allow?: () => void;
  /** Escape. */
  deny: () => void;
  /** The card's own element, read at press time. */
  card: () => HTMLElement | null;
  /** The card's own disclosure is open, so the keys are the menu's. */
  menuOpen: boolean;
}

interface Entry {
  read: () => CardKeys | null;
}

/** Open cards in mount order. The front of the queue mounts first and is the
 *  one the reader is being asked to answer, so it -- not a card waiting behind
 *  it -- owns the keys. */
const open: Entry[] = [];
let bound = false;

/** Everything Tab reaches and Enter or Escape already answers: a nav link, a
 *  button in the header or on a tool card, a menu row, a disclosure. The
 *  transcript is full of them and every one stays reachable while the ask is
 *  up, so the ask takes the key only when nothing of the reader's holds it.
 *
 *  A container parked at `tabindex="-1"` is deliberately NOT one of these: it
 *  holds focus as a scroll region or a landing spot and answers no key of its
 *  own, so the ask still hears the press. */
const HELD_BY =
  'a[href], button, input, textarea, select, summary, [contenteditable], [role="button"], [role="link"], [role="menuitem"], [role="tab"], [role="option"], [role="checkbox"], [role="radio"], [role="switch"], [tabindex]:not([tabindex="-1"])';

/** `closest` rather than a tag test, because a press lands on whatever node
 *  inside the control or the editor actually holds the caret. */
function held(node: Node | null): boolean {
  let el = node instanceof Element ? node : (node?.parentElement ?? null);
  while (el) {
    const owner = el.closest(HELD_BY);
    if (!owner) return false;
    // `contenteditable="false"` is the opposite of an editor: a fixed island
    // inside one. It answers nothing, so it holds nothing -- but the editor
    // around it still might.
    if (owner.getAttribute("contenteditable") !== "false") return true;
    el = owner.parentElement;
  }
  return false;
}

/** Something is open over the card. Its own always-allow menu lives inside it
 *  and is not that something -- `menuOpen` speaks for that one. */
function overlayOver(card: HTMLElement | null): boolean {
  const overlays = document.querySelectorAll<HTMLElement>(
    '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]',
  );
  for (const overlay of overlays) {
    if (!card || !card.contains(overlay)) return true;
  }
  return false;
}

function onKey(event: KeyboardEvent): void {
  if (event.defaultPrevented) return;
  if (event.key !== "Enter" && event.key !== "Escape") return;
  // Mid-composition the key belongs to the IME, which is still assembling a
  // character out of it.
  if (event.isComposing) return;
  // A modifier re-means the key -- it is a shortcut of the host's, not an
  // answer to the ask.
  if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;

  const front = open[0]?.read();
  if (!front || front.menuOpen) return;

  const card = front.card();
  const inCard = (node: Node | null): boolean =>
    card !== null && node !== null && card.contains(node);

  const target = (event.target as Node | null) ?? document.activeElement;
  // The card's own keys are the card's own: it handles them in React, and
  // answering here too would answer twice.
  if (inCard(target)) return;
  // Both the press and the focus, because a key routed at the document still
  // belongs to whatever the reader last tabbed to.
  if (held(target)) return;
  const active = document.activeElement;
  if (!inCard(active) && held(active)) return;
  if (overlayOver(card)) return;

  const answer = event.key === "Enter" ? front.allow : front.deny;
  if (!answer) return;
  event.preventDefault();
  event.stopPropagation();
  answer();
}

/** Bind `keys` to the document for as long as they are offered. Pass null
 *  where the card cannot be answered right now -- it is busy, unavailable,
 *  still waiting on its subject, or already answered -- and nothing is bound,
 *  so a card behind it (or no card at all) is what the keys reach. */
export function useCardKeys(keys: CardKeys | null): void {
  const live = useRef(keys);
  live.current = keys;
  const listening = keys !== null;

  useEffect(() => {
    if (!listening) return;
    const entry: Entry = { read: () => live.current };
    open.push(entry);
    if (!bound) {
      document.addEventListener("keydown", onKey);
      bound = true;
    }
    return () => {
      const at = open.lastIndexOf(entry);
      if (at >= 0) open.splice(at, 1);
      // Nothing is asking any more: the document gets its keys back rather
      // than keeping a listener that only ever decides to do nothing.
      if (open.length === 0 && bound) {
        document.removeEventListener("keydown", onKey);
        bound = false;
      }
    };
  }, [listening]);
}

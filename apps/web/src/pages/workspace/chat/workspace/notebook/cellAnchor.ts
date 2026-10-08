// A link to one cell of a notebook: the page's `#cell=<id>` anchor.
//
// A copied cell link carries it, and a chat's "Go to cell" sets it before it
// opens the notebook's tab. The notebook tab reads it when it opens, and again
// whenever a reveal is asked for while it is already open, and hands it to the
// editor, which brings the cell into view. A tab whose notebook does not hold
// the cell ignores it, so one anchor can never move another notebook.

import { useEffect, useState } from "react";

const ANCHOR = /(?:^|[#&])cell=([0-9a-hjkmnp-tv-z]{10})(?:&|$)/;

/** What a reveal asked for while a tab is open is announced as. */
const REVEAL_EVENT = "alkera:notebook-reveal-cell";

/** The cell a page anchor names, or null. */
export function cellFromHash(hash: string): string | null {
  return ANCHOR.exec(hash)?.[1] ?? null;
}

/** The anchor that names `cellId`. */
export function cellHash(cellId: string): string {
  return `cell=${cellId}`;
}

/** Name `cellId` in the page's anchor and tell any open notebook to bring it
 *  into view. The anchor is replaced, not pushed: going back leaves the page,
 *  not the cell. */
export function revealNotebookCell(cellId: string): void {
  const url = new URL(window.location.href);
  url.hash = cellHash(cellId);
  window.history.replaceState(window.history.state, "", url);
  window.dispatchEvent(new CustomEvent<string>(REVEAL_EVENT, { detail: cellId }));
}

/** The cell this page asks a notebook to reveal: the anchor at mount, then each
 *  reveal after it. `seq` grows on every ask, so the same cell asked for twice
 *  is brought into view twice. */
export function useRevealedCell(): { id: string; seq: number } | null {
  const [asked, setAsked] = useState<{ id: string; seq: number } | null>(() => {
    const id = typeof window === "undefined" ? null : cellFromHash(window.location.hash);
    return id === null ? null : { id, seq: 1 };
  });
  useEffect(() => {
    const ask = (id: string | null) => {
      if (id !== null) setAsked((last) => ({ id, seq: (last?.seq ?? 0) + 1 }));
    };
    const onReveal = (event: Event) => ask((event as CustomEvent<string>).detail);
    const onHash = () => ask(cellFromHash(window.location.hash));
    window.addEventListener(REVEAL_EVENT, onReveal);
    window.addEventListener("hashchange", onHash);
    return () => {
      window.removeEventListener(REVEAL_EVENT, onReveal);
      window.removeEventListener("hashchange", onHash);
    };
  }, []);
  return asked;
}

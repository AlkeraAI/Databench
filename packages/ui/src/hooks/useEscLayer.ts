import { useEffect } from "react";

import { pushEsc } from "./overlayStack";

/**
 * Join the shared Escape stack while `active`. Escape closes exactly ONE surface — the topmost
 * layer — so a host-owned transient (a page's combobox listbox) or a page-level staged back-out
 * must register here rather than bind its own `document` keydown: two raw listeners on one node
 * both fire on a single press, and `stopPropagation` cannot stop a sibling.
 *
 * A page-level handler (registered on mount, below every overlay) receives Escape only when no
 * overlay is open above it — the staged-dismissal order for free.
 *
 * Pass a stable `onEscape` (a `useCallback`, or a closure over a store getter) — a new identity
 * re-registers the layer, which moves it to the top of the stack.
 */
export function useEscLayer(active: boolean, onEscape: () => void): void {
  useEffect(() => {
    if (!active) return;
    return pushEsc(onEscape);
  }, [active, onEscape]);
}

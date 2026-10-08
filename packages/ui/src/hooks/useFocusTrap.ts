import { useEffect, useRef, type RefObject } from "react";

import { pushEsc } from "./overlayStack";

/**
 * The a11y contract shared by every modal surface (the centre Modal and a SidePanel opened in
 * `mode="modal"`): when it is open, focus moves into it, Tab cycles WITHIN it (so the dimmed page
 * behind is never reachable), Escape closes it (via the shared overlay stack so only the topmost
 * surface closes), and focus returns to whatever opened it on close — so a keyboard user is never
 * dropped onto `<body>`. Pair it with `aria-modal="true"` to also hide the background from AT.
 */

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

export interface FocusTrapOptions {
  /** Open AND committed to the DOM — gate this on `usePresence().mounted` so the ref is non-null. */
  active: boolean;
  onClose: () => void;
  /** Escape closes the surface. Default true; pass false for a surface dismissed deliberately. */
  escapeClosable?: boolean;
  /** Focus this on open instead of the first focusable child. Must be visible without
   *  scrolling on open — focus lands with preventScroll. */
  initialFocusRef?: RefObject<HTMLElement | null>;
}

export function useFocusTrap(dialogRef: RefObject<HTMLElement | null>, opts: FocusTrapOptions): void {
  const { active, onClose, escapeClosable = true, initialFocusRef } = opts;

  // Hold onClose in a ref so the effect depends only on `active` — a parent re-render that rebuilds
  // onClose must not re-run the effect and cancel the pending initial focus before it lands.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const escRef = useRef(escapeClosable);
  escRef.current = escapeClosable;
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!active) return;
    const dialog = dialogRef.current;
    if (!dialog) return;

    openerRef.current = document.activeElement as HTMLElement | null;
    const raf = requestAnimationFrame(() => {
      const target = initialFocusRef?.current ?? dialog.querySelector<HTMLElement>(FOCUSABLE);
      // preventScroll: a raw focus() scrolls the page behind the portaled overlay.
      target?.focus({ preventScroll: true });
    });

    // Escape goes through the shared overlay stack so only the TOPMOST surface closes when several
    // are open; binding it here per-dialog would fire every open dialog's handler at once.
    const unEsc = escRef.current ? pushEsc(() => onCloseRef.current()) : undefined;

    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const items = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.offsetParent !== null);
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const activeEl = document.activeElement;
      if (e.shiftKey && activeEl === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && activeEl === last) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener("keydown", onKey);
      unEsc?.();
    };
  }, [active, dialogRef, initialFocusRef]);

  // Restore with preventScroll, unconditionally (React Aria's focusSafely does the same):
  // a re-sorted opener must not yank the viewport; off-screen, the next Tab reveals focus.
  useEffect(() => {
    if (active) return;
    openerRef.current?.focus?.({ preventScroll: true });
  }, [active]);
}

import { useEffect, type RefObject } from "react";

import { pushEsc } from "./overlayStack";

/**
 * A dismissable surface: while `open`, an outside mousedown calls `onClose` (unless
 * `outsidePress: false` — for a hover-owned float that closes on pointer-leave), and (with
 * `escape: true`, the default) Escape closes it THROUGH the shared overlay stack — so a
 * float open above a Modal takes the Escape alone instead of both surfaces closing on one
 * press (two raw `document` listeners both fire on a single press; the stack routes it to
 * the topmost overlay only).
 *
 * `onClose` receives the press target so a caller whose panel is portaled OUTSIDE `ref` (it lives
 * elsewhere in the DOM) can spare a press that landed inside that panel; a caller whose panel sits
 * inside `ref` ignores the argument.
 */
export function useDismiss(
  ref: RefObject<HTMLElement | null>,
  open: boolean,
  onClose: (target?: Node) => void,
  opts: { escape?: boolean; outsidePress?: boolean } = {},
): void {
  const { escape = true, outsidePress = true } = opts;
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose(e.target as Node);
    };
    // Capture phase: a surface like the lineage React Flow canvas stops mousedown propagation on its
    // pane, so a bubble-phase document listener would never fire and the float wouldn't dismiss.
    if (outsidePress) document.addEventListener("mousedown", onDoc, true);
    const unregisterEsc = escape ? pushEsc(() => onClose()) : undefined;
    return () => {
      if (outsidePress) document.removeEventListener("mousedown", onDoc, true);
      unregisterEsc?.();
    };
  }, [ref, open, onClose, escape, outsidePress]);
}

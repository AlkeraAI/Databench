import { useCallback, useEffect, useRef } from "react";

/**
 * An edge fade that shows ONLY when there is more to scroll, on the side that has it. Sets
 * `data-overflow="none|start|end|both"` on the scroll element from scrollLeft / scrollWidth /
 * clientWidth, kept current on scroll AND on resize (a column reflow, or the card body animating
 * open, changes what overflows). Deterministic: the fade cannot paint when nothing is hidden — the
 * leak the pure-CSS `background-attachment: local` trick has when the content already fits.
 */
export function useScrollShadow<T extends HTMLElement>() {
  const ref = useRef<T>(null);

  const update = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const max = el.scrollWidth - el.clientWidth;
    const start = el.scrollLeft > 1;
    const end = el.scrollLeft < max - 1;
    el.dataset.overflow = start && end ? "both" : start ? "start" : end ? "end" : "none";
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    update();
    el.addEventListener("scroll", update, { passive: true });
    // The body opens via a grid-rows transition, so the surface's size changes after mount — a
    // ResizeObserver re-measures through the whole animation, not just once.
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => {
      el.removeEventListener("scroll", update);
      ro.disconnect();
    };
  }, [update]);

  return ref;
}

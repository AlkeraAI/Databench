/**
 * How wide the pane this surface is drawn in actually is.
 *
 * The workspace is a column the reader drags, so every layout decision inside
 * it is about the pane and not about the window — a media query here would
 * answer for a 1280px desktop while the dock itself sits at 395. Measured on
 * the element, re-measured by a `ResizeObserver`, which is the same seam the
 * page's own treegrid measures its scroller through.
 */

import { useLayoutEffect, useState, type RefObject } from "react";

/** Zero until the browser has laid the pane out. Callers treat that as "not
 *  measured yet" rather than as "no room", so the first paint is never the
 *  narrowest layout. */
export function usePaneWidth(ref: RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const read = () => setWidth(element.clientWidth);
    read();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(read);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

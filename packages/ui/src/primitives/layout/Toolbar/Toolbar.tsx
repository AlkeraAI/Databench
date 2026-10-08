import { useLayoutEffect, useRef, type HTMLAttributes, type ReactNode } from "react";

import { cx } from "../../cx";

export interface ToolbarProps extends HTMLAttributes<HTMLDivElement> {
  /** The leading cluster — a scope switch, filters, a count reading. */
  children?: ReactNode;
  /** The trailing cluster, pushed to the right edge — a search field, actions. */
  end?: ReactNode;
}

/**
 * Detect the clusters wrapping onto two rows and mirror it as `data-wrapped` on the root, which the
 * CSS turns into full-row clusters with stretched controls (see toolbar.css). Content-independent:
 * a ResizeObserver re-measures on any root/cluster resize, and each measurement clears the attribute
 * first so it reads the NATURAL single-row layout — the full-row styling it switches on can never
 * latch the toolbar wrapped after the container widens again. The read forces a synchronous reflow
 * inside a rAF tick, so the final attribute always lands before paint (no flash of either state).
 */
function useWrapDetection(enabled: boolean) {
  const rootRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const root = rootRef.current;
    const start = root?.querySelector<HTMLElement>(".alk-toolbar__start");
    const end = root?.querySelector<HTMLElement>(".alk-toolbar__end");
    if (!root) return;
    if (!enabled || !start || !end) {
      root.removeAttribute("data-wrapped");
      return;
    }

    let raf = 0;
    const measure = () => {
      raf = 0;
      root.removeAttribute("data-wrapped");
      // Wrapped = the end cluster's top sits at/below the start cluster's bottom (a straight
      // offsetTop compare would false-positive on two different-height clusters centered in one
      // row). A 0-height start cluster is unmeasured or hidden (no layout yet) — stay un-wrapped
      // until there's a real layout, so a freshly-mounted toolbar doesn't flash into full rows.
      const wrapped = start.offsetHeight > 0 && end.offsetTop >= start.offsetTop + start.offsetHeight;
      if (wrapped) root.setAttribute("data-wrapped", "");
    };
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(measure);
    };
    const observer = new ResizeObserver(schedule);
    observer.observe(root);
    observer.observe(start);
    observer.observe(end);
    schedule();
    return () => {
      observer.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
  }, [enabled]);

  return rootRef;
}

/**
 * Toolbar — the control row above a data surface: a leading cluster (filters, a scope switch, a
 * count) with a trailing cluster (`end` — typically a search) pushed to the right edge. One flex row
 * with the standard gap; when the row gets too narrow the clusters wrap onto their own full rows and
 * their controls stretch to fill them (a scope switch spans row one edge-to-edge, a fixed-width
 * search spans row two) — measured wrap detection sets `data-wrapped` on the root, so a page never
 * hand-rolls a `.toolbar`/`.filters` + `.search { margin-left: auto }` pair or a local breakpoint.
 */
export function Toolbar({ children, end, className, ...rest }: ToolbarProps) {
  const rootRef = useWrapDetection(children != null && end != null);
  return (
    <div ref={rootRef} className={cx("alk-toolbar", className)} {...rest}>
      {children != null ? <div className="alk-toolbar__start">{children}</div> : null}
      {end != null ? <div className="alk-toolbar__end">{end}</div> : null}
    </div>
  );
}

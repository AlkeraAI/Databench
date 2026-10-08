import { IconChevronLeft, IconChevronRight } from "@tabler/icons-react";
import {
  Children,
  Fragment,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactElement,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import type { PaneSpec, SplitPaneProps } from "./types";
import "./splitpane.css";

/** The gap between two panes, in pixels. The grid is written from the matching
 *  custom property in the sheet; the clamp, the collapse threshold and the
 *  squeeze are arithmetic here, so the two have to agree. */
export const SPLIT_GUTTER_PX = 6;

/** The width of the strip a collapsed pane leaves behind, carrying its show
 *  button. Same reason as the gutter: the sheet draws it, this measures with it. */
export const SPLIT_STRIP_PX = 20;

const DEFAULT_STEP = 16;
const DEFAULT_BIG_STEP = 64;
const DEFAULT_COLLAPSE_BELOW = 48;

/** Which side of the filling pane a pane sits on. A pane to the left grows as
 *  its gutter moves right; a pane to the right grows as its gutter moves left. */
type Side = "left" | "right";

interface Measured {
  /** The width each pane is actually drawn at — the host's size, or less when the
   *  container could not hold every minimum. */
  sizes: Map<string, number>;
  /** The widest each pane may be drawn here: its own ceiling, cut by what the
   *  other panes' minimums leave. */
  ceilings: Map<string, number>;
  /** The pane that was drawn narrower than asked, if any. */
  squeezed: string | null;
  /** The grid's column tracks, in visual order. */
  tracks: string[];
  /** The pane each gutter resizes, keyed by the boundary it sits on. */
  controlled: Map<number, PaneSpec>;
  sideOf: Map<string, Side>;
}

function pxOf(spec: PaneSpec): number {
  if (spec.collapsed) return 0;
  return Math.max(spec.min, Math.round(spec.size ?? spec.min));
}

/** Everything the layout needs to draw itself and to answer a resize, derived from
 *  the specs and the width the container turned out to have. Pure: the same specs
 *  and the same width always give the same layout. */
function measure(panes: PaneSpec[], width: number): Measured {
  const fillAt = panes.findIndex((p) => p.fill);
  const fillIndex = fillAt < 0 ? panes.length - 1 : fillAt;

  const sideOf = new Map<string, Side>();
  panes.forEach((spec, i) => sideOf.set(spec.id, i < fillIndex ? "left" : "right"));

  const controlled = new Map<number, PaneSpec>();
  let gutters = 0;
  for (let b = 1; b < panes.length; b += 1) {
    const spec = b - 1 < fillIndex ? panes[b - 1]! : panes[b]!;
    controlled.set(b, spec);
    gutters += spec.collapsed ? SPLIT_STRIP_PX : SPLIT_GUTTER_PX;
  }

  const sizes = new Map<string, number>();
  for (const spec of panes) sizes.set(spec.id, pxOf(spec));

  /** What the panes other than `exclude` claim, including the filling pane's
   *  minimum and every gutter. */
  const claimedBesides = (exclude: string | null): number =>
    panes.reduce(
      (total, spec) =>
        spec.id === exclude ? total : total + (spec.fill && !spec.collapsed ? spec.min : pxOf(spec)),
      gutters,
    );

  let squeezed: string | null = null;
  let over = width > 0 ? claimedBesides(null) - width : 0;
  // The rightmost pane that can be hidden altogether is the one the person is
  // least attached to, so it gives up its width first — narrowed, never hidden:
  // collapsing a pane is theirs to ask for. What it cannot give down to its own
  // minimum the next one leftward gives, so the filling pane keeps its minimum
  // for as long as the side panes have width to spare, rather than being the
  // narrowest column on the screen.
  for (let i = panes.length - 1; i >= 0 && over > 0; i -= 1) {
    const spec = panes[i]!;
    if (spec.fill || spec.collapsed || !spec.collapsible) continue;
    const next = Math.max(spec.min, pxOf(spec) - over);
    if (next < pxOf(spec)) {
      over -= pxOf(spec) - next;
      sizes.set(spec.id, next);
      squeezed ??= spec.id;
    }
  }

  const ceilings = new Map<string, number>();
  for (const spec of panes) {
    const declared =
      spec.max == null
        ? null
        : typeof spec.max === "number"
          ? spec.max
          : width > 0
            ? Math.round((parseFloat(spec.max) / 100) * width)
            : null;
    const available = width > 0 ? width - claimedBesides(spec.id) : null;
    const limits = [declared, available].filter((n): n is number => n != null);
    ceilings.set(
      spec.id,
      limits.length ? Math.max(spec.min, Math.min(...limits)) : Math.max(spec.min, pxOf(spec)),
    );
  }

  const tracks: string[] = [];
  panes.forEach((spec, i) => {
    if (i > 0) {
      const gutterFor = controlled.get(i)!;
      tracks.push(gutterFor.collapsed ? "var(--alkSplitStrip)" : "var(--alkSplitGutter)");
    }
    if (!spec.collapsed) tracks.push(`var(--alk-split-${spec.id})`);
  });

  return { sizes, ceilings, squeezed, tracks, controlled, sideOf };
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), Math.max(low, high));
}

interface Drag {
  paneId: string;
  startX: number;
  startSize: number;
  /** Whether this drag has already collapsed the pane — so it collapses once, and
   *  knows to watch for the pointer coming back. */
  collapsed: boolean;
}

/** IDE-style resizable columns: a gutter between each pair of panes, one pane
 *  taking whatever the others leave, and any pane able to be hidden down to a
 *  strip with a button that brings it back.
 *
 *  The component owns no width. It reports a drag or a keypress and redraws from
 *  the specs it is handed next, so the host decides what a width means and where
 *  it is remembered. */
export function SplitPane({
  label,
  panes,
  onResize,
  onToggle,
  step = DEFAULT_STEP,
  bigStep = DEFAULT_BIG_STEP,
  collapseBelow = DEFAULT_COLLAPSE_BELOW,
  children,
}: SplitPaneProps): ReactElement {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const baseId = useId();
  const [width, setWidth] = useState(0);
  const [drag, setDrag] = useState<Drag | null>(null);
  const dragRef = useRef<Drag | null>(null);

  const layout = measure(panes, width);
  const nodes = Children.toArray(children);

  // The width a double-click returns a pane to: the one its spec first carried,
  // not the one a drag has since left behind.
  const defaults = useRef(new Map<string, number>());
  useEffect(() => {
    for (const spec of panes) {
      if (spec.size != null && !defaults.current.has(spec.id)) {
        defaults.current.set(spec.id, spec.size);
      }
    }
  });

  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const read = (): void => {
      const next = Math.round(el.getBoundingClientRect().width);
      setWidth((prev) => (prev === next ? prev : next));
    };
    read();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(read);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  /** Right-to-left mirrors every horizontal gesture. Read from the marked
   *  ancestor rather than the computed style, which is the way a document
   *  actually declares it. */
  const isRtl = useCallback(
    (): boolean => rootRef.current?.closest("[dir]")?.getAttribute("dir") === "rtl",
    [],
  );

  /** Pixels the pane grows by, per pixel the pointer moves right. */
  const growth = useCallback(
    (paneId: string): number =>
      (layout.sideOf.get(paneId) === "right" ? -1 : 1) * (isRtl() ? -1 : 1),
    [isRtl, layout.sideOf],
  );

  const settle = useCallback(
    (spec: PaneSpec, raw: number): void => {
      const next = clamp(Math.round(raw), spec.min, layout.ceilings.get(spec.id) ?? spec.min);
      if (next !== layout.sizes.get(spec.id)) onResize(spec.id, next);
    },
    [layout.ceilings, layout.sizes, onResize],
  );

  // The pointer listeners are attached once per drag but must read the CURRENT
  // specs on every move, since the host re-renders between them.
  const live = useRef({ panes, layout, settle, growth, onToggle, collapseBelow });
  live.current = { panes, layout, settle, growth, onToggle, collapseBelow };

  useEffect(() => {
    if (!drag) return;
    const move = (event: PointerEvent): void => {
      const active = dragRef.current;
      if (!active) return;
      const now = live.current;
      const spec = now.panes.find((p) => p.id === active.paneId);
      if (!spec) return;
      const raw = active.startSize + now.growth(spec.id) * (event.clientX - active.startX);
      if (spec.collapsible) {
        if (!active.collapsed && raw < spec.min - now.collapseBelow) {
          active.collapsed = true;
          now.onToggle(spec.id, true);
          return;
        }
        if (active.collapsed) {
          if (raw >= spec.min) {
            active.collapsed = false;
            now.onToggle(spec.id, false);
          }
          return;
        }
      }
      now.settle(spec, raw);
    };
    const end = (): void => {
      dragRef.current = null;
      setDrag(null);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
    };
  }, [drag]);

  const startDrag = (spec: PaneSpec, event: ReactPointerEvent<HTMLDivElement>): void => {
    if (event.button !== 0) return;
    event.preventDefault();
    // Capture keeps the moves coming while the cursor crosses a pane; where it is
    // unavailable the window listeners still see them.
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const next: Drag = {
      paneId: spec.id,
      startX: event.clientX,
      startSize: layout.sizes.get(spec.id) ?? spec.min,
      collapsed: false,
    };
    dragRef.current = next;
    setDrag(next);
  };

  const onKey = (spec: PaneSpec, event: ReactKeyboardEvent<HTMLDivElement>): void => {
    const size = layout.sizes.get(spec.id) ?? spec.min;
    const move = event.shiftKey ? bigStep : step;
    const grow = growth(spec.id);
    switch (event.key) {
      case "ArrowRight":
        event.preventDefault();
        settle(spec, size + grow * move);
        return;
      case "ArrowLeft":
        event.preventDefault();
        settle(spec, size - grow * move);
        return;
      case "Home":
        event.preventDefault();
        settle(spec, spec.min);
        return;
      case "End":
        event.preventDefault();
        settle(spec, layout.ceilings.get(spec.id) ?? spec.min);
        return;
      case "Enter":
      case " ":
        if (!spec.collapsible) return;
        event.preventDefault();
        onToggle(spec.id, !spec.collapsed);
        return;
      default:
    }
  };

  const style: CSSProperties & Record<string, string> = {
    gridTemplateColumns: layout.tracks.join(" "),
  };
  for (const spec of panes) {
    if (spec.collapsed) continue;
    style[`--alk-split-${spec.id}`] = spec.fill
      ? "minmax(0, 1fr)"
      : `${layout.sizes.get(spec.id) ?? spec.min}px`;
  }

  const paneDomId = (id: string): string => `${baseId}-${id}`;

  return (
    <div
      ref={rootRef}
      className="alk-split"
      role="group"
      aria-label={label}
      style={style}
      data-dragging={drag ? "" : undefined}
      data-squeezed={layout.squeezed ?? undefined}
    >
      {panes.map((spec, index) => {
        const gutterFor = index > 0 ? layout.controlled.get(index)! : null;
        // While a drag is in flight the gutter stays a gutter even after the pane
        // it controls has collapsed, so pulling back the other way brings it in.
        const asStrip = gutterFor?.collapsed && drag?.paneId !== gutterFor.id;
        const side = gutterFor ? layout.sideOf.get(gutterFor.id) : undefined;
        return (
          <Fragment key={spec.id}>
            {gutterFor ? (
              asStrip ? (
                <button
                  type="button"
                  className="alk-split__strip"
                  aria-label={`Show ${gutterFor.label}`}
                  onClick={() => onToggle(gutterFor.id, false)}
                >
                  {side === "right" ? <IconChevronLeft /> : <IconChevronRight />}
                </button>
              ) : (
                <div
                  className="alk-split__gutter"
                  role="separator"
                  tabIndex={0}
                  aria-orientation="vertical"
                  aria-controls={paneDomId(gutterFor.id)}
                  aria-label={`Resize ${gutterFor.label}`}
                  aria-valuenow={layout.sizes.get(gutterFor.id) ?? gutterFor.min}
                  aria-valuemin={gutterFor.min}
                  aria-valuemax={layout.ceilings.get(gutterFor.id) ?? gutterFor.min}
                  data-active={drag?.paneId === gutterFor.id ? "" : undefined}
                  onPointerDown={(event) => startDrag(gutterFor, event)}
                  onDoubleClick={() => {
                    const home = defaults.current.get(gutterFor.id);
                    if (home != null) settle(gutterFor, home);
                  }}
                  onKeyDown={(event) => onKey(gutterFor, event)}
                />
              )
            ) : null}
            {spec.collapsed ? null : (
              <div className="alk-split__pane" id={paneDomId(spec.id)} data-pane-id={spec.id}>
                {nodes[index]}
              </div>
            )}
          </Fragment>
        );
      })}
    </div>
  );
}

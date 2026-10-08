import { useCallback, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

/**
 * The shared hover mechanism every viz reuses (Sparkline, MiniBars): it maps the pointer's x within
 * the plotted element to the nearest sample and returns that sample's anchor, so a floating tooltip
 * pins to the point, not the cursor. A sample's x is a FRACTION of the plot width (0..1) — a bar
 * chart passes bar centres so the tip lands on the bar, not an evenly-spaced grid the bars miss.
 * The pure cores (`sampleFractions`, `nearestIndex`) are unit-tested directly.
 */

/** Even line layout: `count` samples edge-to-edge (i/(count-1)); a lone sample sits at centre. */
export function sampleFractions(count: number): number[] {
  if (count <= 1) return count === 1 ? [0.5] : [];
  return Array.from({ length: count }, (_, i) => i / (count - 1));
}

/** The sample closest to `relX` px in a `width`-px plot given each sample's fractional x. Strict `<`
 *  keeps the earlier index on a tie; clamps to a valid index at either edge. */
export function nearestIndex(relX: number, width: number, fractions: number[]): number {
  if (fractions.length === 0 || width <= 0) return 0;
  const frac = relX / width;
  let best = 0;
  let bestDist = Infinity;
  for (let i = 0; i < fractions.length; i++) {
    const d = Math.abs(fractions[i] - frac);
    if (d < bestDist) {
      bestDist = d;
      best = i;
    }
  }
  return best;
}

/** The active sample's anchor — a thin column at its x. `pointY` (a line's plotted y) and `color`
 *  (the resolved series colour) let a point marker sit on the value and paint correctly even though
 *  it renders in a portal outside the `--alk-viz-color` scope. */
export interface VizAnchorRect {
  left: number;
  top: number;
  width: number;
  height: number;
  pointY?: number;
  color?: string;
}

export interface VizHover {
  /** The sample under the pointer, or null when the pointer is away. */
  activeIndex: number | null;
  /** The anchor for the active sample (a thin column at its x), or null when away. */
  anchorRect: VizAnchorRect | null;
  /** Spread onto the plotted SVG element. */
  hoverProps: {
    ref: (el: SVGSVGElement | null) => void;
    onPointerMove: (e: ReactPointerEvent) => void;
    onPointerLeave: () => void;
  };
  /** Force-clear the active sample. */
  clear: () => void;
}

export interface UseVizHoverOptions {
  /** Per-sample fractional x-positions (0..1). Omit for the even line layout. Pass bar centres for
   *  a bar chart so the anchor lands on the bar. */
  fractions?: number[];
  /** Per-sample fractional y-positions (0..1, 0 = top). Supply for a line so a point marker can sit
   *  on the plotted value; omit for a bar chart (the bar highlight carries the emphasis). */
  yFractions?: number[];
}

/** Track the sample nearest the pointer over a `count`-sample viz. Inert (never activates) when
 *  `count` < 2 or `enabled` is false, so a caller can gate the whole mechanism on having a tooltip
 *  renderer without branching its own JSX. */
export function useVizHover(count: number, enabled = true, opts: UseVizHoverOptions = {}): VizHover {
  const elRef = useRef<SVGSVGElement | null>(null);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [anchorRect, setAnchorRect] = useState<VizAnchorRect | null>(null);

  const custom = opts.fractions;
  const yFractions = opts.yFractions;
  const fractions = useMemo(() => custom ?? sampleFractions(count), [custom, count]);

  const setRef = useCallback((el: SVGSVGElement | null) => {
    elRef.current = el;
  }, []);

  const clear = useCallback(() => {
    setActiveIndex(null);
    setAnchorRect(null);
  }, []);

  const onPointerMove = useCallback(
    (e: ReactPointerEvent) => {
      if (!enabled || count < 2) return;
      const el = elRef.current;
      if (!el) return;
      const box = el.getBoundingClientRect();
      const relX = e.clientX - box.left;
      const idx = nearestIndex(relX, box.width, fractions);
      // Anchor a thin column at the sample's x so the tip pins to the point, not the cursor.
      const pointX = box.left + fractions[idx] * box.width;
      const pointY = yFractions ? box.top + yFractions[idx] * box.height : undefined;
      // Capture the resolved series colour so a portaled point marker (outside --alk-viz-color's
      // scope) still paints in it; only needed when a marker will be drawn.
      const color = yFractions
        ? getComputedStyle(el).getPropertyValue("--alk-viz-color").trim() || undefined
        : undefined;
      setActiveIndex(idx);
      setAnchorRect({ left: pointX, top: box.top, width: 1, height: box.height, pointY, color });
    },
    [count, enabled, fractions, yFractions],
  );

  return {
    activeIndex,
    anchorRect,
    hoverProps: { ref: setRef, onPointerMove, onPointerLeave: clear },
    clear,
  };
}

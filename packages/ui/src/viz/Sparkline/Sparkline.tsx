import { useId, useMemo, useRef, type ReactNode } from "react";

import { cx } from "../../primitives/cx";
import { vizToneStyle, type VizTone } from "../tone";
import { useVizHover } from "../useVizHover";
import { VizTooltip } from "../VizTooltip";

// An area+line trend over a small series. The series colour is caller-set via --alk-viz-color, or a
// `tone` from the core vocabulary (which sets that same var to the tone's solid token).
//
// The viewBox carries a one-unit gutter on each side of the 0-100 plot: the stroke is drawn in
// screen space (non-scaling) and centred on the sample, so a plot that ran edge to edge had its
// first and last samples half-clipped by the svg's own box. Against a card's padding that read as
// a stray vertical stub pinned to the card edge rather than as the end of a line.
// An empty / all-zero series renders a single muted baseline labelled "No data" rather than a flat
// zero-line that reads as a real measured trend at zero. Pass a string `width` (e.g. "100%") + a
// `className` to make it fill a cell.
//
// Pass `tooltip` to make it interactive: as the pointer sweeps the line, the nearest sample is
// highlighted with a dot and a floating readout shows whatever the caller renders for that
// (value, index). The tooltip mechanism is shared across the viz layer (useVizHover + VizTooltip),
// so every viz hovers, flips, and animates the same way.
/** The painted box: the 0-100 plot plus a one-unit gutter for the screen-space stroke. */
const VIEW_BOX = "-1 0 102 32";

export function Sparkline({
  points,
  width = 88,
  height = 44,
  className,
  tone,
  tooltip,
}: {
  points: number[];
  width?: number | string;
  height?: number;
  className?: string;
  /** Series tone — sets `--alk-viz-color` to the tone's solid token. Unset, the ambient var flows. */
  tone?: VizTone;
  /** Render the tooltip body for the hovered sample. Omit for a static, non-interactive spark. */
  tooltip?: (value: number, index: number) => ReactNode;
}) {
  const id = useId();
  const wrapRef = useRef<HTMLSpanElement | null>(null);
  const hasData = points.length > 1 && points.some((v) => v > 0);
  // The plotted y of each sample as a fraction of the viewBox height (0 = top), so the hover marker
  // sits on the value. Recomputed only when the series changes.
  const yFractions = useMemo(() => {
    if (points.length < 2) return undefined;
    const lo = Math.min(...points);
    const range = Math.max(...points) - lo || 1;
    return points.map((v) => (31 - ((v - lo) / range) * 30) / 32);
  }, [points]);
  // The sample's x as a fraction of the PAINTED box, which is a unit wider than the plot on each
  // side — without the gutter's share the marker drifts off the line it is supposed to sit on.
  const xFractions = useMemo(() => {
    if (points.length < 2) return undefined;
    return points.map((_, i) => (1 + (i / (points.length - 1)) * 100) / 102);
  }, [points]);
  const hover = useVizHover(points.length, Boolean(tooltip) && hasData, {
    fractions: xFractions,
    yFractions,
  });

  if (!hasData) {
    return (
      <svg
        className={cx("alk-viz alk-spark", className)}
        data-state="empty"
        viewBox={VIEW_BOX}
        preserveAspectRatio="none"
        width={width}
        height={height}
        style={vizToneStyle(tone)}
        role="img"
        aria-label="No data"
      >
        <path className="alk-spark__line" data-state="empty" d="M0,31 L100,31" fill="none" />
      </svg>
    );
  }
  const max = Math.max(...points);
  const min = Math.min(...points);
  const span = max - min || 1;
  const x = (i: number) => (i / (points.length - 1)) * 100;
  const y = (v: number) => 31 - ((v - min) / span) * 30; // 1px top/bottom breathing room
  const line = points.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(" ");
  const area = `${line} L100,32 L0,32 Z`;
  const active = hover.activeIndex;

  const svg = (
    <svg
      ref={tooltip ? hover.hoverProps.ref : undefined}
      className={cx("alk-viz alk-spark", className)}
      viewBox={VIEW_BOX}
      preserveAspectRatio="none"
      width={width}
      height={height}
      style={vizToneStyle(tone)}
      role="img"
      aria-label="Trend, last period"
      onPointerMove={tooltip ? hover.hoverProps.onPointerMove : undefined}
      onPointerLeave={tooltip ? hover.hoverProps.onPointerLeave : undefined}
    >
      <defs>
        <linearGradient id={id} x1="0" x2="0" y1="0" y2="1">
          <stop className="alk-spark__stop0" offset="0%" />
          <stop className="alk-spark__stop1" offset="100%" />
        </linearGradient>
      </defs>
      <path className="alk-spark__area" d={area} fill={`url(#${id})`} />
      <path className="alk-spark__line" d={line} fill="none" />
    </svg>
  );

  if (!tooltip) return svg;

  const portalTarget =
    (wrapRef.current?.closest("[data-theme], [data-theme-group]") as HTMLElement | null) ?? undefined;

  return (
    <span ref={wrapRef} className="alk-viz-anchor">
      {svg}
      <VizTooltip anchorRect={hover.anchorRect} portalTarget={portalTarget}>
        {active != null ? tooltip(points[active], active) : null}
      </VizTooltip>
    </span>
  );
}

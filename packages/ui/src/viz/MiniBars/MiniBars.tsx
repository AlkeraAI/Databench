import { useMemo, useRef, type ReactNode } from "react";

import { cx } from "../../primitives/cx";
import { vizToneStyle, type VizTone } from "../tone";
import { useVizHover } from "../useVizHover";
import { VizTooltip } from "../VizTooltip";

// A small bar chart for periodic counts. The series colour is caller-set via --alk-viz-color, or a
// `tone` from the core vocabulary (which sets that same var to the tone's solid token).
// An empty / all-zero series renders a single muted baseline track labelled "No data" rather than
// a real-looking zero-height bar, so "nothing measured" reads as nothing, not as zero. Pass a string
// `width` (e.g. "100%") + a `className` to make it fill a cell.
//
// Pass `tooltip` to make it interactive: the bar under the pointer is highlighted and a floating
// readout shows whatever the caller renders for that (value, index). Shares the viz layer's tooltip
// mechanism (useVizHover + VizTooltip) with Sparkline.
export function MiniBars({
  values,
  width = 88,
  height = 44,
  className,
  tone,
  tooltip,
}: {
  values: number[];
  width?: number | string;
  height?: number;
  className?: string;
  /** Series tone — sets `--alk-viz-color` to the tone's solid token. Unset, the ambient var flows. */
  tone?: VizTone;
  /** Render the tooltip body for the hovered bar. Omit for a static, non-interactive chart. */
  tooltip?: (value: number, index: number) => ReactNode;
}) {
  const wrapRef = useRef<HTMLSpanElement | null>(null);
  const hasData = values.some((v) => v > 0);
  const n = values.length;
  const gap = 4;
  // A lone bar at full width reads as a solid block, so a single value renders as one centered
  // column instead (the mini echo of the over-time chart's single-day column).
  const bw = n === 1 ? 36 : (100 - gap * (n - 1)) / n;
  const x0 = n === 1 ? (100 - bw) / 2 : 0;
  // Anchor the tooltip on each bar's centre (as a fraction of the 100-unit viewBox width) so the tip
  // pins to the bar the pointer is over, not to an evenly-spaced grid the bars don't sit on.
  const centers = useMemo(
    () => values.map((_, i) => (x0 + i * (bw + gap) + bw / 2) / 100),
    [values, x0, bw],
  );
  const hover = useVizHover(n, Boolean(tooltip) && hasData, { fractions: centers });

  if (!hasData) {
    return (
      <svg
        className={cx("alk-viz alk-bars", className)}
        data-state="empty"
        viewBox="0 0 100 32"
        preserveAspectRatio="none"
        width={width}
        height={height}
        style={vizToneStyle(tone)}
        role="img"
        aria-label="No data"
      >
        <rect className="alk-bars__track" x={0} y={30.5} width={100} height={1.5} rx="0.75" />
      </svg>
    );
  }
  const max = Math.max(...values) || 1;
  const active = hover.activeIndex;

  const svg = (
    <svg
      ref={tooltip ? hover.hoverProps.ref : undefined}
      className={cx("alk-viz alk-bars", className)}
      viewBox="0 0 100 32"
      preserveAspectRatio="none"
      width={width}
      height={height}
      style={vizToneStyle(tone)}
      role="img"
      aria-label="Recent additions"
      onPointerMove={tooltip ? hover.hoverProps.onPointerMove : undefined}
      onPointerLeave={tooltip ? hover.hoverProps.onPointerLeave : undefined}
    >
      {values.map((v, i) => {
        const bx = x0 + i * (bw + gap);
        const bh = Math.max(1.5, (v / max) * 30);
        return (
          <g key={i}>
            <rect className="alk-bars__track" x={bx} y={0} width={bw} height={32} rx="1" />
            <rect
              className="alk-bars__val"
              data-state={i === active ? "active" : undefined}
              x={bx}
              y={32 - bh}
              width={bw}
              height={bh}
              rx="1"
            />
          </g>
        );
      })}
    </svg>
  );

  if (!tooltip) return svg;

  const portalTarget =
    (wrapRef.current?.closest("[data-theme], [data-theme-group]") as HTMLElement | null) ?? undefined;

  return (
    <span ref={wrapRef} className="alk-viz-anchor">
      {svg}
      <VizTooltip anchorRect={hover.anchorRect} portalTarget={portalTarget}>
        {active != null ? tooltip(values[active], active) : null}
      </VizTooltip>
    </span>
  );
}

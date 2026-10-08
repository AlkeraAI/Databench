import { cx } from "../../primitives/cx";
import { vizToneStyle, type VizTone } from "../tone";

// A donut progress meter — a fraction drawn as a swept arc. Sized to sit inside a stat card's
// right edge. The series colour is caller-set via --alk-viz-color (falls back to the brand), or a
// `tone` from the core vocabulary (which sets that same var to the tone's solid token).
export function Ring({
  pct,
  size = 44,
  className,
  tone,
}: {
  pct: number;
  size?: number;
  className?: string;
  /** Series tone — sets `--alk-viz-color` to the tone's solid token. Unset, the ambient var flows. */
  tone?: VizTone;
}) {
  const r = 15;
  const circ = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(1, pct));
  return (
    <svg
      className={cx("alk-viz alk-ring", className)}
      viewBox="0 0 36 36"
      width={size}
      height={size}
      style={vizToneStyle(tone)}
      role="img"
      aria-label={`${Math.round(clamped * 100)}% used`}
    >
      <circle className="alk-ring__track" cx="18" cy="18" r={r} fill="none" strokeWidth="3" />
      <circle
        className="alk-ring__ind"
        cx="18"
        cy="18"
        r={r}
        fill="none"
        strokeWidth="3"
        strokeDasharray={circ}
        strokeDashoffset={circ * (1 - clamped)}
        strokeLinecap="round"
        transform="rotate(-90 18 18)"
      />
    </svg>
  );
}

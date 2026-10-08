import type { CSSProperties, HTMLAttributes } from "react";

import { cx } from "../../cx";

export type MeterTone = "brand" | "neutral" | "info" | "success" | "warning" | "danger";

export interface MeterProps extends Omit<HTMLAttributes<HTMLDivElement>, "role"> {
  /** Filled fraction, 0..1 (clamped). A value over 1 (an overspend) clamps to a full bar — pair it
   *  with `tone="danger"` to signal the overrun. */
  value: number;
  /** Bar ink: `brand` (default) for a plain proportion, `neutral` for a de-emphasised grey gauge,
   *  `info` for an informational reading, `warning` / `danger` for a nearing / over limit,
   *  `success` for a healthy reading. */
  tone?: MeterTone;
  /** Track height on the 8px scale: `sm` (4px, default — a row gauge), `md` (6px), `lg` (8px). */
  size?: "sm" | "md" | "lg";
  /** Accessible name for the progressbar (e.g. "Credits used"). */
  label?: string;
  /** Interior graduation ticks over the track (a measured, graduated-vessel reading) — `3` engraves
   *  quarter marks. Omit for a plain gauge. */
  ticks?: number;
}

/**
 * Meter — a horizontal proportion gauge. A rounded track holds a toned fill scaled to `value` (0..1).
 * The fill animates via `transform: scaleX` (never width), so it rides the compositor and honours the
 * motion floor; the track clips it to the rounded ends. Exposes the `progressbar` role with
 * aria-valuenow / min / max for assistive tech. Extra props forward to the track element.
 */
export function Meter({ value, tone = "brand", size = "sm", label, ticks, className, style, ...rest }: MeterProps) {
  const fraction = Math.max(0, Math.min(1, value));
  const segments = ticks != null && ticks > 0 ? ticks + 1 : null;
  return (
    <div
      className={cx("alk-meter", className)}
      data-size={size !== "sm" ? size : undefined}
      role="progressbar"
      aria-label={label}
      aria-valuenow={Math.round(fraction * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      style={
        {
          ...style,
          "--alk-meter-v": fraction,
          // A ready percentage, not a count: `calc(100% / var(--n))` fails to parse (division
          // by a var), so the stylesheet consumes this directly.
          ...(segments != null ? { "--alk-meter-seg": `${100 / segments}%` } : null),
        } as CSSProperties
      }
      {...rest}
    >
      <div className={cx("alk-meter__fill", `alk-meter__fill--${tone}`)} />
      {segments != null ? <div className="alk-meter__ticks" aria-hidden /> : null}
    </div>
  );
}

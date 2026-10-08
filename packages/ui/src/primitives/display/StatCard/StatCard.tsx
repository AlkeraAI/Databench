import type { HTMLAttributes, ReactNode } from "react";

import { cx } from "../../cx";

// A KPI card: the label + an optional trend chip ride the top; the measurement (mono, largest type)
// and an optional visualization pin to the bottom row, so the card fills its height instead of
// floating short. A plain-language note sits under the value. Viz-agnostic — the caller passes any
// node into the `viz` slot; trend is a slot too (the caller owns its icon + sentiment). Extra props
// (className, data-* hooks) forward to the root <article>.
export interface StatCardProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  label: ReactNode;
  value: ReactNode;
  note?: ReactNode;
  trend?: ReactNode;
  viz?: ReactNode;
  /** Pin the value + viz row to the card's bottom (default true -- a tall viz needs the room
   *  above). Pass false when the card sits in a row of no-viz siblings, so every card's value
   *  stays right under its label and the row keeps one shared value line. */
  pinViz?: boolean;
}

export function StatCard({ label, value, note, trend, viz, pinViz = true, className, ...rest }: StatCardProps) {
  return (
    <article className={cx("alk-statcard", className)} data-has-viz={viz && pinViz ? "" : undefined} {...rest}>
      <div className="alk-statcard__head">
        <span className="alk-statcard__label">{label}</span>
        {trend}
      </div>
      <div className="alk-statcard__main">
        <span className="alk-statcard__value">{value}</span>
        {viz}
      </div>
      {note != null ? <span className="alk-statcard__note">{note}</span> : null}
    </article>
  );
}

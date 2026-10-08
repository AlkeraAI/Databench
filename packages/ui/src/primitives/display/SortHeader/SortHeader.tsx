import { forwardRef, type ButtonHTMLAttributes } from "react";

import { cx } from "../../cx";
import { ChevronDownIcon } from "../../icons";

import "./sortheader.css";

export type SortDirection = "asc" | "desc";

export interface SortHeaderProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onClick" | "type"> {
  /** True on the column the table is currently sorted by — full-ink label + visible caret. */
  active?: boolean;
  /** The current sort direction. The caret points down for `desc` and flips up for `asc`; on an
   *  inactive header it previews the direction a click would apply. */
  direction?: SortDirection;
  /** Fired on click. The consumer owns the sort state (which column, which direction). */
  onSort: () => void;
  /** `end` fills the cell and right-justifies the label, so the header sits over its right-aligned
   *  (numeric) readings. Default `start`. */
  align?: "start" | "end";
}

/** The sortable column-header button — rendered as the CONTENT of a Table `<th>` (the Table
 *  primitive has no sort feature, so the sort affordance lives on this button). The label plus a
 *  direction caret that shows on the active column (faintly on hover elsewhere) and flips with
 *  `direction`.
 *
 *  Accessibility: the button's accessible name carries the sort state itself (visually-hidden
 *  "sorted ascending/descending" suffix). This component cannot reach its host cell, so the
 *  CONSUMER's `<th>` should ALSO set `aria-sort="ascending" | "descending"` on the active column
 *  (and omit it on the rest) whenever the table markup allows it. */
export const SortHeader = forwardRef<HTMLButtonElement, SortHeaderProps>(function SortHeader(
  { active = false, direction = "asc", onSort, align = "start", className, children, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      className={cx("alk-sortheader", className)}
      data-active={active ? "true" : undefined}
      data-align={align === "end" ? "end" : undefined}
      onClick={onSort}
      {...rest}
    >
      {children}
      {active ? <span className="alk-sortheader__state" data-vh>sorted {direction === "asc" ? "ascending" : "descending"}</span> : null}
      <span className="alk-sortheader__caret" data-dir={direction} aria-hidden="true">
        <ChevronDownIcon size={12} />
      </span>
    </button>
  );
});

import type { ReactNode } from "react";

import { usePager } from "../../../hooks";
import { cx } from "../../cx";

/** Accessible names for the pager controls — exported so consumers and tests reference these, never
 *  a copied literal. */
export const TABLE_PAGER_LABELS = {
  previous: "Previous page",
  next: "Next page",
  pageInput: "Page number",
} as const;

export interface TablePagerProps {
  /** Current page, 1-based. */
  page: number;
  /** Total pages; clamped to >= 1. */
  pageCount: number;
  /** Commit a page move; the value is clamped to [1, pageCount] before this fires. */
  onPage: (page: number) => void;
  className?: string;
}

export interface TableFooterPager {
  page: number;
  pageCount: number;
  onPage: (page: number) => void;
}

export interface TableFooterProps {
  /** The count, range, or selection reading at the footer's left edge. */
  start?: ReactNode;
  /** A complete controlled pager. Navigation is omitted when it has only one page. */
  pager?: TableFooterPager;
  className?: string;
}

/**
 * The Table footer's navigation, standalone: the type-a-page number ("Page [n] of N") and the
 * prev / next steppers. `Table` mounts it in its own footer; a surface that pages a non-table
 * ledger (the lineage record's schema panel) mounts the same control instead of re-rolling the
 * anatomy. Fully controlled — the caller owns `page`; the paging state machine is `usePager`.
 */
export function TablePager({
  page,
  pageCount,
  onPage,
  className,
}: TablePagerProps) {
  const pager = usePager({ page, pageCount, onPage });

  return (
    <div className={cx("alk-table__pager", className)}>
      <label className="alk-table__pagelabel">
        Page
        <input
          className="alk-table__pageinput"
          type="text"
          inputMode="numeric"
          aria-label={TABLE_PAGER_LABELS.pageInput}
          {...pager.input}
        />
        of {pager.pages}
      </label>
      <button
        type="button"
        className="alk-table__pagebtn"
        aria-label={TABLE_PAGER_LABELS.previous}
        {...pager.prev}
      >
        ‹
      </button>
      <button
        type="button"
        className="alk-table__pagebtn"
        aria-label={TABLE_PAGER_LABELS.next}
        {...pager.next}
      >
        ›
      </button>
    </div>
  );
}

/** The base Table footer, reusable by table-shaped ledgers that own custom row DOM. */
export function TableFooter({ start, pager, className }: TableFooterProps) {
  const showPager = pager != null && pager.pageCount > 1;
  if (start == null && !showPager) return null;
  return (
    <div className={cx("alk-table__foot", className)}>
      {start != null ? (
        <div className="alk-table__footstart">{start}</div>
      ) : null}
      {showPager ? <TablePager {...pager} /> : null}
    </div>
  );
}

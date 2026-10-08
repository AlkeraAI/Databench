import type { ReactNode } from "react";
import { IconChevronLeft, IconChevronRight } from "@tabler/icons-react";

import { usePager } from "../../hooks";
import { Button, cx, TABLE_PAGER_LABELS } from "../../primitives";
import "./datatable.css";

export interface DataTablePagerProps {
  /** Current page, 1-based. */
  page: number;
  /** Total number of pages (clamped to ≥ 1). */
  pageCount: number;
  /** Commit a page change; the value is clamped to [1, pageCount] before this fires. */
  onPage: (page: number) => void;
  /** Left-of-pager content — typically a row count or range ("1,234 rows"). */
  count?: ReactNode;
  className?: string;
}

/** The footer pager shared by the chat data grid (`DataTable`) and the full blob
 *  view (`BlobView`): a row-count slot on the left, then a type-a-page input
 *  ("Page [n] of N") and prev/next buttons on the right. Fully controlled — the
 *  parent owns `page`, the paging state machine is `usePager` — so the same
 *  chrome drives a client-sliced table and a server-paged one. The nav is hidden
 *  for a single page (nothing to walk); a lone `count` still renders so a small
 *  table keeps its row count. */
export function DataTablePager({ page, pageCount, onPage, count, className }: DataTablePagerProps) {
  const pager = usePager({ page, pageCount, onPage });
  const multi = pager.pages > 1;

  if (count == null && !multi) return null;
  return (
    <div className={cx("alk-datatable-pager", className)}>
      {count != null ? <div className="alk-datatable-pager__count">{count}</div> : null}
      {multi ? (
        <div className="alk-datatable-pager__nav">
          <label className="alk-datatable-pager__pagelabel">
            Page
            <input
              className="alk-datatable-pager__pageinput"
              type="text"
              inputMode="numeric"
              aria-label={TABLE_PAGER_LABELS.pageInput}
              {...pager.input}
            />
            of {pager.pages.toLocaleString()}
          </label>
          <Button
            iconOnly
            variant="secondary"
            fill="ghost"
            size="sm"
            aria-label={TABLE_PAGER_LABELS.previous}
            {...pager.prev}
          >
            <IconChevronLeft size="var(--alkIconSm)" />
          </Button>
          <Button
            iconOnly
            variant="secondary"
            fill="ghost"
            size="sm"
            aria-label={TABLE_PAGER_LABELS.next}
            {...pager.next}
          >
            <IconChevronRight size="var(--alkIconSm)" />
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/**
 * The row under the last child: how much of the folder is on screen, and the
 * three ways to see more.
 *
 * It is a treegrid row rather than a bar under the grid so the count stays in
 * the reading order a screen reader walks, right after the rows it describes.
 */

import type { SoftThreshold } from "./useSoftThreshold";

/** One locale for the whole footer: the count in the sentence and the count in
 *  the progress line must never disagree about how a thousand is written. */
const NUMBER = new Intl.NumberFormat("en-US");

export interface ThresholdFooterProps extends Pick<
  SoftThreshold,
  "loaded" | "total" | "exact" | "hasMore" | "loadingAll" | "loadMore" | "loadAll" | "cancelLoadAll"
> {
  /** True while no total is knowable — a folder too new to have been aggregated
   *  whose listing has not ended. Optional so a caller that always knows its
   *  total need not pass it. */
  counting?: boolean;
  /** How many of the loaded rows the listing keeps out of sight (hidden
   *  files, a chat folder's records): they count toward the folder, not
   *  toward what is shown. */
  hidden?: number;
  /** How many columns the row spans, for `aria-colspan`. */
  columnCount?: number;
}

/**
 * "20,000 of about 1,204,311 shown" — "about" only while the aggregate may lag.
 * Rows a listing keeps out of sight are counted out of what is shown and named
 * beside it ("3 of 5 shown · 2 hidden"), so the count matches the rows drawn.
 *
 * With no total at all the sentence says so rather than reading as a bare count:
 * a folder created a moment ago has no aggregate until the first aggregation
 * pass, and until its listing ends the rows on screen are a floor, not the
 * answer. "counting…" is the honest word for that, and it appears ONLY there —
 * the moment the listing ends, the rows themselves are the exact total.
 */
export function describeShown(
  loaded: number,
  total: number | null,
  exact: boolean,
  counting = false,
  hidden = 0,
): string {
  const kept = Math.min(Math.max(hidden, 0), loaded);
  const shown = NUMBER.format(loaded - kept);
  const aside = kept > 0 ? ` · ${NUMBER.format(kept)} hidden` : "";
  if (total === null) return counting ? `${shown} shown · counting…` : `${shown} shown${aside}`;
  if (exact) return `${shown} of ${NUMBER.format(total)} shown${aside}`;
  return `${shown} of about ${NUMBER.format(total)} shown${aside}`;
}

export function ThresholdFooter({
  loaded,
  total,
  exact,
  counting = false,
  hasMore,
  loadingAll,
  loadMore,
  loadAll,
  cancelLoadAll,
  hidden = 0,
  columnCount = 5,
}: ThresholdFooterProps) {
  return (
    <div className="alk-files-threshold" role="row" aria-live="polite">
      <div className="alk-files-threshold__cell" role="gridcell" aria-colspan={columnCount}>
        <span className="alk-files-threshold__count">
          {/* A listing that ended with nothing in it says so, rather than "0 of 0 shown". */}
          {loaded === 0 && !hasMore && !counting && !loadingAll && (total === 0 || total === null)
            ? "This folder is empty."
            : describeShown(loaded, total, exact, counting, hidden)}
        </span>
        {loadingAll ? (
          <>
            <span className="alk-files-threshold__progress">
              Loading all · {NUMBER.format(loaded)} so far
            </span>
            <button type="button" className="alk-files-threshold__action" onClick={cancelLoadAll}>
              Cancel
            </button>
          </>
        ) : (
          hasMore && (
            <>
              <button type="button" className="alk-files-threshold__action" onClick={loadMore}>
                Load more
              </button>
              <button type="button" className="alk-files-threshold__action" onClick={loadAll}>
                Load all
              </button>
            </>
          )
        )}
      </div>
    </div>
  );
}

export default ThresholdFooter;

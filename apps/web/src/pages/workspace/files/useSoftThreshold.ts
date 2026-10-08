/**
 * The soft threshold: the listing prefetches up to a ceiling, then stops asking.
 *
 * A folder with a million children is not a wall — every child stays reachable.
 * The threshold only decides when the browser stops *prefetching on scroll*: it
 * pages the listing in markers of 500 until 20,000 rows are loaded, then hands
 * the decision to the person through the footer. `Load more` raises
 * the ceiling by another 20,000; `Load all` lifts it entirely and keeps paging
 * in the background until the last marker; cancelling pins the ceiling at what
 * is already loaded, so the rows fetched so far stay on screen.
 *
 * The count in the footer is NOT the number of rows loaded: it comes from the
 * folder's aggregated `dir_stats.direct_children`, which may lag one aggregation
 * interval, so it reads as "about N" until the last page comes back without a
 * marker — at that point the rows themselves are the exact total.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  CHILDREN_PAGE,
  flattenChildren,
  useChildren,
  useItem,
  type ChildrenOptions,
  type Item,
} from "@/api/files";

import { dirStatsOf } from "@/lib/files/columns";
import { FILES_SOFT_THRESHOLD_ROWS, useLimits } from "@/lib/limits";

/** How many children the browser loads before it stops prefetching and asks. A host
 *  narrows it through `LimitsProvider`; this is what it is without one. */
export const SOFT_THRESHOLD = FILES_SOFT_THRESHOLD_ROWS;

export interface SoftThreshold {
  /** Every row loaded so far, in server order. */
  rows: Item[];
  loaded: number;
  /** The folder's direct children, from `dir_stats` — or the loaded count once
   *  the listing has reached its end. Null when neither is known yet. */
  total: number | null;
  /** True once the last marker page came back: `total` is then the truth, not
   *  an aggregate that may lag. */
  exact: boolean;
  /** No total is knowable yet: a freshly created folder has no aggregate until
   *  the first aggregation pass, and the listing has not reached its end, so
   *  the rows on screen are a floor rather than the count. */
  counting: boolean;
  /** More children exist beyond what is loaded. */
  hasMore: boolean;
  /** A `Load all` is paging in the background. */
  loadingAll: boolean;
  /** A page is in flight (either the scroll prefetch or a background load). */
  isFetching: boolean;
  loadMore: () => void;
  loadAll: () => void;
  cancelLoadAll: () => void;
}

export interface SoftThresholdOptions extends ChildrenOptions {
  /** The first ceiling. Unset, the host's limit decides. */
  threshold?: number;
}

/** What a paged listing hands the pager: the marker query's own three facts. */
export interface PagedQuery {
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => unknown;
}

/** The paging half of the soft threshold, over any keyset listing.
 *
 * It is separated from the folder listing so a second paged view — the trash —
 * pages the same way rather than growing a pager of its own: prefetch while the
 * budget allows, then hand the decision to the person through the same footer.
 * `reset` is whatever identifies the listing; a change to it starts a fresh
 * budget, so one view's `Load all` never keeps paging the next one.
 */
export function useCeilingPager(
  query: PagedQuery,
  threshold: number,
  loaded: number,
  reset: unknown,
): Pick<
  SoftThreshold,
  "hasMore" | "loadingAll" | "loadMore" | "loadAll" | "cancelLoadAll"
> {
  const [ceiling, setCeiling] = useState(threshold);
  const [loadingAll, setLoadingAll] = useState(false);

  useEffect(() => {
    setCeiling(threshold);
    setLoadingAll(false);
  }, [reset, threshold]);

  const { hasNextPage, isFetchingNextPage, fetchNextPage } = query;

  // One page at a time, and only while the budget allows it. `isFetchingNextPage`
  // gates the effect so a re-render mid-flight cannot ask for the same marker twice.
  useEffect(() => {
    if (!hasNextPage || isFetchingNextPage) return;
    if (loaded >= ceiling) return;
    void fetchNextPage();
  }, [hasNextPage, isFetchingNextPage, loaded, ceiling, fetchNextPage]);

  // The end of the listing ends the background load; there is nothing left to page.
  useEffect(() => {
    if (!hasNextPage && loadingAll) setLoadingAll(false);
  }, [hasNextPage, loadingAll]);

  const loadMore = useCallback(() => {
    setCeiling((current) => Math.max(current, loaded) + threshold);
  }, [loaded, threshold]);

  const loadAll = useCallback(() => {
    setLoadingAll(true);
    setCeiling(Number.POSITIVE_INFINITY);
  }, []);

  /** Stop where we are. The ceiling drops to what is loaded rather than back to
   *  the original threshold, so cancelling never discards a fetched page. */
  const cancelLoadAll = useCallback(() => {
    setLoadingAll(false);
    setCeiling(loaded);
  }, [loaded]);

  return { hasMore: hasNextPage, loadingAll, loadMore, loadAll, cancelLoadAll };
}

export function useSoftThreshold(
  driveId: string | undefined,
  parentId: string | undefined,
  options: SoftThresholdOptions = {},
): SoftThreshold {
  const limits = useLimits();
  const { threshold = limits.filesSoftThresholdRows, ...readOptions } = options;

  const children = useChildren(driveId, parentId, {
    ...readOptions,
    limit: readOptions.limit ?? CHILDREN_PAGE,
  });
  const rows = useMemo(() => flattenChildren(children.data), [children.data]);
  const loaded = rows.length;

  const parent = useItem(driveId, parentId, readOptions);
  const hasMore = children.hasNextPage === true;

  // A different folder starts a fresh budget: the previous folder's `Load all`
  // must not keep paging the one the person navigated to.
  const pager = useCeilingPager(
    {
      hasNextPage: hasMore,
      isFetchingNextPage: children.isFetchingNextPage,
      fetchNextPage: children.fetchNextPage,
    },
    threshold,
    loaded,
    `${driveId ?? ""}/${parentId ?? ""}`,
  );
  const { loadingAll, loadMore, loadAll, cancelLoadAll } = pager;

  // A folder created a moment ago carries no `dir_stats` yet. Its own listing is
  // the answer as soon as it ends — the rows ARE the direct children — so the
  // total falls back to the listing rather than reading as nothing.
  const aggregate = parent.data ? (dirStatsOf(parent.data)?.directChildren ?? null) : null;
  const exact = !hasMore && children.isSuccess;
  const total = exact ? loaded : aggregate;

  return {
    rows,
    loaded,
    total,
    exact,
    counting: total === null,
    hasMore,
    loadingAll,
    isFetching: children.isFetching,
    loadMore,
    loadAll,
    cancelLoadAll,
  };
}

export default useSoftThreshold;

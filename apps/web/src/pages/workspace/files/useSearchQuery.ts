/**
 * The search read behind the toolbar field.
 *
 * Three things make it feel like a search field rather than a form:
 *
 *  * **Debounce.** The text is debounced 150 ms, so a burst of keystrokes is one
 *    request rather than one per character.
 *  * **Cancellation.** The debounced text rides the query key, so each new text
 *    is a new cache entry and react-query aborts the in-flight one; the signal
 *    it hands the query function is passed straight to `fetch`, so the old
 *    request is cancelled on the wire and cannot land after the new one.
 *  * **The API is the authority.** The server filters before it paginates, but
 *    the hook still drops any row whose capabilities say the caller cannot read
 *    it: a result the user is not authorized for must never reach the treegrid,
 *    whatever the response contains.
 *
 * The scope is the folder on screen by default and the whole drive on one
 * click, and the filter chips reach the server through the same
 * `toChildrenParams` the folder listing uses, so a chip cannot mean one thing
 * in a listing and another in a search.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  toChildrenParams,
  type ChildrenPage,
  type Item,
  type ListFilters,
  type OrderBy,
} from "@/api/files";
import { apiBaseUrl, apiFetch, failedResponse } from "@/api/client";
import { keys } from "@/api/keys";
import { displayPath } from "@/lib/files/columns";
import type { SearchScope } from "./filterState";
import { FILES_SEARCH_DEBOUNCE_MS, FILES_SEARCH_MIN_LENGTH, useLimits } from "@/lib/limits";

/** Results stream in as the user types, once the typing settles. A host narrows the
 *  window through `LimitsProvider`; this is what it is without one. */
export const SEARCH_DEBOUNCE_MS = FILES_SEARCH_DEBOUNCE_MS;

/** Below this a search would match most of the drive; the field waits instead of
 *  asking the server for everything. */
export const MIN_SEARCH_LENGTH = FILES_SEARCH_MIN_LENGTH;

/** The wire spelling of the search text and of "only under this folder". The
 *  server owns this grammar — `GET /files/drives/{id}/search` parses `scope` as
 *  `drive` or `folder:{id}` and refuses anything else — so it is named once here
 *  and pinned against the route's own code in the tests. */
export const SEARCH_TEXT_PARAM = "q";
export const SEARCH_SCOPE_PARAM = "scope";
export const SEARCH_FOLDER_SCOPE_PREFIX = "folder:";

/** Hold `value` back until it has been still for `delay` ms. */
export function useDebounced<T>(value: T, delay: number = SEARCH_DEBOUNCE_MS): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return settled;
}

/**
 * Whether the caller may see this row.
 *
 * A row that says nothing about its capabilities is shown — the server already
 * authorized the listing it came from. A row that explicitly says the caller
 * cannot read it is dropped, because the item is the API's own statement about
 * what the caller may see and the UI must not overrule it upward.
 */
export function isReadable(item: Item): boolean {
  const caps = item.capabilities as
    | (Record<string, unknown> & { can_read?: boolean })
    | undefined
    | null;
  if (!caps) return true;
  const declared = caps.can_read ?? (caps as { canRead?: boolean }).canRead;
  return declared !== false;
}

/** Only the rows the caller may see, in the order the server returned them. */
export function readableItems(items: readonly Item[] | undefined): Item[] {
  return (items ?? []).filter(isReadable);
}

/** The folder a result lives in, as the results list shows it: the path minus
 *  the file's own name. The drive root reads as "/". */
export function enclosingPath(item: Item): string {
  const full = displayPath(item) ?? "";
  if (!full) return "/";
  const cut = full.lastIndexOf("/");
  if (cut <= 0) return "/";
  return full.slice(0, cut);
}

export interface SearchQueryOptions {
  driveId: string | undefined;
  /** The folder on screen; the scope when `scope` is `"folder"`. */
  folderId: string | undefined;
  scope: SearchScope;
  /** The raw field text. The hook debounces it. */
  text: string;
  filters?: ListFilters;
  orderBy?: OrderBy;
  /** Test seam: the debounce window, so a test can drive it deterministically. */
  debounceMs?: number;
}

export interface SearchQueryResult {
  /** The authorized rows, ready for the treegrid. */
  items: Item[];
  isLoading: boolean;
  isFetching: boolean;
  error: Error | null;
  /** True once the text is long enough that a request is being made. */
  isActive: boolean;
  /** The text the results on screen actually answer. */
  settledText: string;
}

/** The search route the generated document declares, with the drive filled in. */
export function searchPath(driveId: string): string {
  return `/api/v1/files/drives/${encodeURIComponent(driveId)}/search`;
}

/**
 * Search the drive (or one folder), debounced and cancellable.
 *
 * The request is made directly rather than through the generated client because
 * `scope` is undeclared in the document — but the *response* is still the
 * generated `ChildrenPage`, so a shape change lands here as a type error and the
 * results render through exactly the same rows a listing produces.
 */
export function useSearchQuery(options: SearchQueryOptions): SearchQueryResult {
  const { driveId, folderId, scope, text, filters, orderBy } = options;
  const limits = useLimits();
  const settledText = useDebounced(text.trim(), options.debounceMs ?? limits.filesSearchDebounceMs);
  const scopeFolderId = scope === "folder" ? folderId : undefined;

  const params = {
    ...toChildrenParams(filters, orderBy),
    [SEARCH_TEXT_PARAM]: settledText,
    // Drive scope is the absence of the parameter, which the server reads the
    // same way it reads `scope=drive`.
    ...(scopeFolderId
      ? { [SEARCH_SCOPE_PARAM]: `${SEARCH_FOLDER_SCOPE_PREFIX}${scopeFolderId}` }
      : {}),
  };
  const isActive = Boolean(driveId) && settledText.length >= limits.filesSearchMinLength;

  const query = useQuery<ChildrenPage>({
    queryKey: keys.files.search(driveId, params),
    enabled: isActive,
    queryFn: async ({ signal }) => {
      const search = new URLSearchParams();
      for (const [name, value] of Object.entries(params)) {
        if (value !== undefined) search.set(name, value);
      }
      const response = await apiFetch(
        `${apiBaseUrl}${searchPath(driveId ?? "")}?${search.toString()}`,
        { credentials: "include", signal },
      );
      if (!response.ok) throw await failedResponse(response);
      return (await response.json()) as ChildrenPage;
    },
  });

  return {
    items: readableItems(query.data?.value),
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    error: query.error ?? null,
    isActive,
    settledText,
  };
}

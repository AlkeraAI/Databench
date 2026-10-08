// The one predicate for "the server has already told us this is not there".
//
// A chat somebody deleted in another tab, a folder that was trashed, a node a
// link points at that no longer exists: each of those is an ANSWER. Asking
// again costs a request and gets the same one back, and the page is already
// showing the dead end. The event stream on a busy box invalidates the chat and
// files families many times a minute, so without this a single deleted chat
// turns into a 404 every few seconds for as long as the tab stays open.

import { ApiError } from "./errors";
import type { Query } from "@tanstack/react-query";

/** The statuses that mean the thing being read is not coming back. A 403 is
 *  deliberately NOT one of them — a grant can be given, and the read should see
 *  it when it is. */
const GONE_STATUSES: ReadonlySet<number> = new Set([404, 410]);

export function isGone(error: unknown): boolean {
  return error instanceof ApiError && GONE_STATUSES.has(error.status);
}

/** `invalidateQueries` predicate: everything except a read the server has
 *  already answered with "gone". Used everywhere an invalidation is fired at a
 *  PREFIX — the mutation policy and the event stream's scheduler — so no caller
 *  has to remember which of the keys under it may be dead. */
export function notGone(query: Query): boolean {
  return !isGone(query.state.error);
}

// Re-reading what a machine is writing, at a rate the API can carry.
//
// A box holding a chat's folder saves as fast as the agent writes: a file
// appended every second raises a node frame per save and a lease frame or two
// per report. Answered one re-read per frame, a chat page with its Files pane
// open cost the server close to three hundred requests a minute, and every
// tab open on that chat cost the same again. These frames all say the same
// small thing (a file the reader can already see changed its bytes), so the
// reads they ask for go through one throttle per query client: the first
// change after a quiet spell is read at once, and the rest of a burst lands in
// one trailing read per key per window, however many surfaces asked for it.
//
// A frame names the node's version (its etag after the change), which is
// enough to drop a read outright when the cache already holds that version.
// And a file this tab is co-editing live already shows its own write-backs:
// its item is only marked stale, so a later read is fresh, and nothing is
// fetched for it while the editor is up.

import { hashKey, type InfiniteData, type QueryClient, type QueryKey } from "@tanstack/react-query";

import type { ChildrenPage, Item } from "@/api/files";
import { keys } from "@/api/keys";
import { MACHINE_REFRESH_MS } from "@/lib/limits";

import type { RealtimeEventFrame } from "./eventMap";

/** The reasons a node frame carries when a machine (or a live document's
 *  write-back) changed the node's bytes, which arrive at the rate it saves.
 *  Any other node frame is a person's action (a rename, a share, a trash) and
 *  is refreshed at once and widely. */
export const MACHINE_RATE_NODE_REASONS: ReadonlySet<string> = new Set([
  "live_saved",
  "inbound_superseded",
  "live_batch",
  "conflict",
  "live_doc_saved",
]);

/** The reason a live document's own write-back carries. */
export const LIVE_DOC_SAVED = "live_doc_saved";

/** Whether `frame` is one a machine raises at the rate it saves: a node
 *  frame with one of the reasons above, or any lease frame (the holder's
 *  reports of what it is doing to the files under it). */
export function isMachineRate(frame: RealtimeEventFrame): boolean {
  if (frame.type === "file_lease.changed") return true;
  return frame.type === "file_node.changed" && MACHINE_RATE_NODE_REASONS.has(frame.reason ?? "");
}

export interface ThrottleTimers {
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
  now: () => number;
}

const DEFAULT_TIMERS: ThrottleTimers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
  now: () => Date.now(),
};

export interface RefreshThrottle {
  /** Re-read `queryKey` (as a prefix) now if it has not been read in the
   *  window, else once when the window ends. */
  request(queryKey: QueryKey): void;
  /** Drop every trailing read still waiting. */
  dispose(): void;
}

/** One throttle over `queryClient`: per key, a leading read and at most one
 *  trailing read per `windowMs`. */
export function createRefreshThrottle(
  queryClient: QueryClient,
  opts: { windowMs?: number; timers?: ThrottleTimers } = {},
): RefreshThrottle {
  const windowMs = opts.windowMs ?? MACHINE_REFRESH_MS;
  const timers = opts.timers ?? DEFAULT_TIMERS;
  const lastRead = new Map<string, number>();
  const trailing = new Map<string, unknown>();

  const read = (hash: string, queryKey: QueryKey): void => {
    trailing.delete(hash);
    lastRead.set(hash, timers.now());
    queryClient.invalidateQueries({ queryKey }).catch(() => undefined);
  };

  return {
    request(queryKey) {
      const hash = hashKey(queryKey);
      if (trailing.has(hash)) return;
      const wait = (lastRead.get(hash) ?? -Infinity) + windowMs - timers.now();
      if (wait <= 0) {
        read(hash, queryKey);
        return;
      }
      trailing.set(
        hash,
        timers.setTimeout(() => read(hash, queryKey), wait),
      );
    },
    dispose() {
      for (const handle of trailing.values()) timers.clearTimeout(handle);
      trailing.clear();
      lastRead.clear();
    },
  };
}

const perClient = new WeakMap<QueryClient, RefreshThrottle>();

/** The throttle every surface on `queryClient` shares, so the event bridge, an
 *  open folder and an open file asking for the same read make it once. */
export function machineRefresh(queryClient: QueryClient): RefreshThrottle {
  let found = perClient.get(queryClient);
  if (found === undefined) {
    found = createRefreshThrottle(queryClient);
    perClient.set(queryClient, found);
  }
  return found;
}

/** Replace the shared throttle for `queryClient` (tests pin its clock). */
export function setMachineRefresh(queryClient: QueryClient, throttle: RefreshThrottle): void {
  perClient.get(queryClient)?.dispose();
  perClient.set(queryClient, throttle);
}

const liveHeld = new Map<string, number>();

/** Say this tab co-edits `nodeId` live until the returned release is called:
 *  the live document shows its own write-backs, so they fetch nothing here. */
export function holdLiveNode(nodeId: string): () => void {
  liveHeld.set(nodeId, (liveHeld.get(nodeId) ?? 0) + 1);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const left = (liveHeld.get(nodeId) ?? 1) - 1;
    if (left > 0) liveHeld.set(nodeId, left);
    else liveHeld.delete(nodeId);
  };
}

export function isHeldLive(nodeId: string): boolean {
  return liveHeld.has(nodeId);
}

/** Whether a version tag is at least `version`. Tags the server sends are the
 *  node's etag as a decimal; anything else is never treated as current. */
function atLeast(etag: string | undefined, version: number): boolean {
  if (etag === undefined || version <= 0 || !/^\d+$/.test(etag)) return false;
  return Number(etag) >= version;
}

/** The cache already holds the node's item at the frame's version. */
export function itemIsCurrent(queryClient: QueryClient, nodeId: string, version: number): boolean {
  return atLeast(queryClient.getQueryData<Item>(keys.files.item(nodeId))?.etag, version);
}

/** Every cached listing of `parentId` already shows `nodeId` at the frame's
 *  version (and there is at least one). A listing without the row has to be
 *  read: the node may be new to it. */
export function listingIsCurrent(
  queryClient: QueryClient,
  driveId: string | undefined,
  parentId: string,
  nodeId: string,
  version: number,
): boolean {
  const cached = queryClient.getQueriesData<InfiniteData<ChildrenPage>>({
    queryKey: keys.files.childrenOf(driveId, parentId),
  });
  if (cached.length === 0) return false;
  return cached.every(([, data]) => {
    const row = data?.pages.flatMap((page) => page.value).find((one) => one.id === nodeId);
    return atLeast(row?.etag, version);
  });
}

/**
 * Ask for what a machine-rate node frame changed: the node's item and the
 * listing it sits in, each through the shared throttle and each skipped when
 * the cache already holds the frame's version. `listing` is the listing to
 * re-read, when the caller has one (the bridge passes the frame's parent; a
 * surface passes only the folders it has open).
 */
export function refreshForSave(
  queryClient: QueryClient,
  frame: RealtimeEventFrame,
  opts: { item?: boolean; listing?: { driveId: string | undefined; parentId: string } } = {},
): void {
  const throttle = machineRefresh(queryClient);
  const nodeId = frame.entity_id;
  if (opts.item !== false && !itemIsCurrent(queryClient, nodeId, frame.version)) {
    if (frame.reason === LIVE_DOC_SAVED && isHeldLive(nodeId)) {
      queryClient
        .invalidateQueries({ queryKey: keys.files.item(nodeId), refetchType: "none" })
        .catch(() => undefined);
    } else {
      throttle.request(keys.files.item(nodeId));
    }
  }
  const listing = opts.listing;
  if (
    listing !== undefined &&
    !listingIsCurrent(queryClient, listing.driveId, listing.parentId, nodeId, frame.version)
  ) {
    throttle.request(keys.files.childrenOf(listing.driveId, listing.parentId));
  }
}

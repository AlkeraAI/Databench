// Keeping an open folder in step with the machine that is writing it.
//
// A leased folder is being written from somewhere else, once per save. The
// bridge's event map already refreshes the folder a frame names, and that is
// the whole drive's answer — this hook is the one folder's: it listens for the
// frames that belong to THIS live root and the folders the reader actually has
// open, refreshes exactly those listings, and answers what the header should
// say about the root.
//
// Three things make that safe to run over a machine saving continuously:
//   - a frame for a folder nobody has open is ignored, so a deep write costs
//     nothing;
//   - frames coalesce, so twenty saves in a quarter-second cost one refetch of
//     one folder rather than twenty of the drive, and a machine saving for
//     minutes costs one read per folder per `MACHINE_REFRESH_MS`;
//   - with the stream down the browser cannot learn about a save at all, so it
//     falls back to a slow poll of the open folders AND stops claiming to be
//     live (the caller reads `streamDown` into the copy). A browser that says
//     it is offline is down from that moment, not from the stalled stream's
//     timeout a minute later.
//
// And the root is re-read whenever what it last said may have gone stale with
// no frame to say so: the stream coming back, the browser coming back online,
// the machine holding it changing reachability, and (slowly, on the same poll)
// for as long as it reads as offline. A clean resume of the stream replays the
// events it missed, but a lease that went offline and came back while nothing
// was saved emits nothing, and the footer read "Offline since …" long after
// the machine was back.

import { useCallback, useEffect, useMemo, useRef } from "react";
import { hashKey, useQueryClient, type QueryClient, type QueryKey } from "@tanstack/react-query";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { useFrames } from "@/api/events/frameBus";
import { isMachineRate, machineRefresh, refreshForSave } from "@/api/events/machineRefresh";
import { useRealtimeDown } from "@/api/events/status";
import { useBrowserOnline } from "@/lib/online";
import { useItem, type Item } from "@/api/files";
import { keys } from "@/api/keys";
import { readsPaused } from "@/api/readGate";

import { liveState, type FolderLiveness } from "../liveRoot/liveness";
import { useNow } from "../useLeaseFacet";

/** Frames landing inside this window are refreshed together. The same window
 *  the bridge coalesces on, so a save that emits a node frame and a lease frame
 *  is one pass either way. */
export const LIVE_COALESCE_MS = 250;

/** How often the open folders are re-read while the event stream is down. Slow
 *  on purpose: it is the fallback for a stream that is expected back, not a
 *  second delivery mechanism. */
export const LIVE_POLL_MS = 15_000;

/** Lease frames held while the root's lease is not yet known, one per leased
 *  node. A few are plenty: the one this listing lives under is among the
 *  latest, and each is only a cue to re-read. */
export const MAX_HELD_LEASE_FRAMES = 16;

export interface FolderLivenessView {
  /** What the root folder's header should say it is showing. */
  liveness: FolderLiveness;
  /** The event stream is not delivering: the view is a saved copy whatever the
   *  lease says, and the poll below is what keeps it moving. */
  streamDown: boolean;
  /** The browser says it has no network: the reason the stream is down. */
  offline: boolean;
  /** Re-read the root and every open folder now. */
  refresh: () => void;
}

/** The leased node a lease frame is about. The payload names it; a server that
 *  names it only as the entity reads the same. */
const leaseNodeOf = (frame: RealtimeEventFrame): string => frame.lease_node_id ?? frame.entity_id;

/** Before the root has been read there is no lease to judge and no time to
 *  name — the header reads as the saved copy rather than flickering "Live". */
const NOT_LOADED: FolderLiveness = { state: "persisted", asOf: null, reason: "no-lease" };

/**
 * Live state for one leased root, plus the refreshes that keep the folders
 * under it current.
 *
 * `openFolderIds` is every folder whose listing is mounted (the root, and any
 * expanded folder under it). `onBox` is the count of rows in view the machine
 * is holding back — the facet on the root cannot know it, so the caller that
 * has the rows passes it in.
 */
export function useFolderLiveness(
  driveId: string | undefined,
  rootNodeId: string | undefined,
  openFolderIds: readonly string[],
  onBox = 0,
  /** Whether the machine holding the root is reachable, when the caller knows
   *  (the chat page does; the Files page does not). The verdict is the
   *  server's; a change here only re-reads the root so the server's word on
   *  the lease is current. */
  holderReady: boolean | undefined = undefined,
): FolderLivenessView {
  const queryClient = useQueryClient();
  const offline = !useBrowserOnline();
  const streamDown = useRealtimeDown() || offline;
  const root = useItem(driveId, rootNodeId);
  const now = useNow();

  // Read through a ref inside the predicate: the set of open folders changes as
  // the reader expands and collapses, and re-subscribing on every change would
  // drop the frames that land in the gap.
  const open = useRef<readonly string[]>(openFolderIds);
  open.current = openFolderIds;

  const pending = useRef(new Map<string, QueryKey>());
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flush = useCallback(() => {
    timer.current = null;
    const batch = [...pending.current.values()];
    pending.current.clear();
    for (const queryKey of batch) {
      queryClient.invalidateQueries({ queryKey }).catch(() => undefined);
    }
  }, [queryClient]);

  const queue = useCallback(
    (queryKey: QueryKey) => {
      pending.current.set(hashKey(queryKey), queryKey);
      if (timer.current === null) timer.current = setTimeout(flush, LIVE_COALESCE_MS);
    },
    [flush],
  );

  // The lease this listing lives under: the root's own, or the nearest leased
  // folder above it, as the facet every node under a lease names. The Files
  // page lists whatever folder the reader walked into, and the frames name the
  // leased folder, so matching the listed folder alone left a subfolder of a
  // chat's folder deaf to its own lease.
  const leasedAt = useRef<string | undefined>(undefined);
  leasedAt.current = root.data?.lease?.node_id ?? undefined;

  // Which lease this listing lives under is known only once the caller has
  // named the root and the root has been read. A lease frame that lands before
  // then cannot be judged, and dropping it would leave the rows on whatever the
  // first read said until the next save. So it is held, the latest per leased
  // node, and judged the moment the lease is known.
  const resolved = rootNodeId !== undefined && root.data !== undefined;
  const leaseKnown = useRef(resolved);
  leaseKnown.current = resolved;
  const held = useRef(new Map<string, RealtimeEventFrame>());

  const ours = useCallback(
    (leased: string): boolean =>
      leased === rootNodeId || (leasedAt.current !== undefined && leased === leasedAt.current),
    [rootNodeId],
  );

  const apply = useCallback(
    (frame: RealtimeEventFrame): void => {
      // A node frame moved one folder's listing and nothing else. A lease
      // frame moved the in-flight plane, which rides the root's own item AND
      // every row under it (a `subtree` frame, a batch that touched too many
      // folders to name each, says the same of the listings themselves): the
      // chip on a row is the plane's word about that row, and the states that
      // move without a save (a file reported as uploading, one left on the
      // machine, a row settled) announce themselves only this way. Refreshing
      // the root alone left every chip where it was until the next save
      // happened to land beside it.
      //
      // Both arrive at the rate the machine saves, so they go through the
      // throttle the whole client shares (a person's rename or trash is still
      // read at once). The event bridge asks the same throttle for the same
      // listing, so the two cost one read between them.
      const drive = driveId ?? frame.drive_id;
      if (frame.type !== "file_lease.changed") {
        if (frame.parent_id === undefined) return;
        if (isMachineRate(frame)) {
          refreshForSave(queryClient, frame, {
            item: false,
            listing: { driveId: drive, parentId: frame.parent_id },
          });
        } else {
          queue(keys.files.childrenOf(drive, frame.parent_id));
        }
        return;
      }
      const throttle = machineRefresh(queryClient);
      throttle.request(keys.files.item(rootNodeId));
      for (const folderId of open.current) {
        throttle.request(keys.files.childrenOf(drive, folderId));
      }
    },
    [queue, queryClient, driveId, rootNodeId],
  );

  useFrames(
    (frame) => {
      if (frame.type === "file_lease.changed") {
        return !leaseKnown.current || ours(leaseNodeOf(frame));
      }
      if (frame.type !== "file_node.changed") return false;
      return frame.parent_id !== undefined && open.current.includes(frame.parent_id);
    },
    (frame) => {
      if (frame.type === "file_lease.changed" && !leaseKnown.current) {
        const leased = leaseNodeOf(frame);
        held.current.delete(leased);
        held.current.set(leased, frame);
        if (held.current.size > MAX_HELD_LEASE_FRAMES) {
          const oldest = held.current.keys().next().value;
          if (oldest !== undefined) held.current.delete(oldest);
        }
        return;
      }
      apply(frame);
    },
  );

  useEffect(() => {
    if (!resolved || held.current.size === 0) return;
    const waiting = [...held.current.entries()];
    held.current.clear();
    for (const [leased, frame] of waiting) {
      if (ours(leased)) apply(frame);
    }
  }, [resolved, ours, apply]);

  // Drop a queued pass on unmount: invalidating for a screen that is gone is a
  // request nobody is waiting for.
  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = null;
      pending.current.clear();
    },
    [],
  );

  const refresh = useCallback(() => {
    refreshNow(queryClient, driveId, rootNodeId, open.current);
  }, [queryClient, driveId, rootNodeId]);

  // Coming back from something that may have left the root's word stale: the
  // stream recovering (or the network), or the machine's reachability moving.
  // The first render is not a change and reads nothing extra.
  const seen = useRef<{ streamDown: boolean; holderReady: boolean | undefined } | null>(null);
  useEffect(() => {
    const was = seen.current;
    seen.current = { streamDown, holderReady };
    if (was === null) return;
    const recovered = was.streamDown && !streamDown;
    const moved =
      was.holderReady !== undefined && holderReady !== undefined && was.holderReady !== holderReady;
    if (recovered || moved) refreshNow(queryClient, driveId, rootNodeId, open.current);
  }, [streamDown, holderReady, queryClient, driveId, rootNodeId]);

  const item = root.data;
  const liveness = useMemo(
    () => (item ? liveState(item, now, onBox) : NOT_LOADED),
    [item, now, onBox],
  );
  // A lease that reads offline changes back with no frame when its machine
  // returns, so it is re-read on the slow poll for as long as it says so.
  const stale = liveness.state === "persisted" && liveness.reason === "offline";

  useEffect(() => {
    if (!streamDown && !stale) return;
    const handle = setInterval(() => {
      // This poll is an interval, not a query, so nothing on the query layer
      // slows it down when the server says it is being asked too often — and it
      // is the drive's busiest read while the stream is down. It reads the gate
      // itself: an invalidation inside the pause would only queue a refetch
      // that waits at it, and the tick after the pause re-reads everything
      // anyway, so nothing is lost by skipping this one.
      if (readsPaused()) return;
      refreshNow(queryClient, driveId, rootNodeId, open.current);
    }, LIVE_POLL_MS);
    return () => clearInterval(handle);
  }, [streamDown, stale, queryClient, driveId, rootNodeId]);

  return { liveness, streamDown, offline, refresh };
}

function refreshNow(
  queryClient: QueryClient,
  driveId: string | undefined,
  rootNodeId: string | undefined,
  openFolderIds: readonly string[],
): void {
  queryClient.invalidateQueries({ queryKey: keys.files.item(rootNodeId) }).catch(() => undefined);
  for (const folderId of openFolderIds) {
    queryClient
      .invalidateQueries({ queryKey: keys.files.childrenOf(driveId, folderId) })
      .catch(() => undefined);
  }
}

/** The rows the machine is holding back, counted off a listing the caller has
 *  in view. Both states mean "written on the box and not sent": one because the
 *  file is too big to stream, one because the bandwidth window is spent. */
export function countOnBox(rows: readonly Item[]): number {
  return rows.filter((row) => {
    const state = row.live?.state;
    return state === "on_box" || state === "deferred";
  }).length;
}

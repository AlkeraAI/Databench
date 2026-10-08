// One node, kept current while a machine is writing it.
//
// A file open in a tab is read once and then left alone — re-reading it on a
// timer would cost a request per tab per tick for a file nobody is touching.
// So it re-reads on exactly one trigger: a frame that names THIS node. A frame
// for its neighbour, its parent, or another lease is not its business.
//
// It also has to answer the other half honestly. A file can stop existing while
// it is open — trashed from another window, removed by the machine — and a tab
// that keeps rendering the last copy it saw is a lie the reader acts on. `gone`
// is that answer, and it covers both spellings the server has for it: the node
// is not there (404) or it is in the trash.

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { ApiError } from "@/api/errors";
import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { useFrames } from "@/api/events/frameBus";
import { isMachineRate, machineRefresh, refreshForSave } from "@/api/events/machineRefresh";
import { useItem, type Item } from "@/api/files";
import { keys } from "@/api/keys";

/**
 * What the machine is doing to ONE node, as the wire carries it.
 *
 * Taken from the generated schema rather than hand-typed: the set of states is
 * the server's, and a state it adds (or stops sending) has to be a typecheck
 * failure at the reader, not a chip that renders a word nobody wrote copy for.
 */
export type LiveFacet = components["schemas"]["LiveFacet"];

export interface LiveNodeView {
  /** The node as it was last read, or undefined until it has been. */
  item: Item | undefined;
  /** Its version tag, which moves for any change to the node. */
  etag: string | null;
  /** Its content tag, which moves only when the bytes do — a share, a rename,
   *  a move or a trash leaves it alone. */
  ctag: string | null;
  /** What the machine is doing to it right now, when it is doing anything. */
  live: LiveFacet | null;
  /** It is not in the drive any more: trashed, or answered 404. */
  gone: boolean;
  /** Re-read it now. */
  refresh: () => void;
}

/** Whether a frame is about this node. A node frame names it as the entity. A
 *  lease frame names the leased folder: it is this node's business when the
 *  node IS that folder, or when the node sits under it and the caller said so
 *  (`leaseNodeId`) — the plane's word about a file (writing, uploading, left on
 *  the machine, settled) rides the file's own item and is announced only by
 *  the lease frame, so a tab that ignored it kept the old word until the next
 *  save happened to land. */
function namesNode(
  frame: RealtimeEventFrame,
  nodeId: string | undefined,
  leaseNodeId: string | undefined,
): boolean {
  if (nodeId === undefined) return false;
  if (frame.type === "file_node.changed") return frame.entity_id === nodeId;
  if (frame.type === "file_lease.changed") {
    const leased = frame.lease_node_id ?? frame.entity_id;
    return leased === nodeId || (leaseNodeId !== undefined && leased === leaseNodeId);
  }
  return false;
}

/** One node's live view. `leaseNodeId` is the folder whose lease covers the
 *  node, when the caller knows it: a frame for that lease re-reads the node. */
export function useLiveNode(
  driveId: string | undefined,
  nodeId: string | undefined,
  leaseNodeId?: string,
): LiveNodeView {
  const queryClient = useQueryClient();
  // A 404 here is an answer, not a flake: the node this tab was opened on is
  // gone, and asking twice more only delays saying so.
  const query = useItem(driveId, nodeId, { settled404: true });

  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: keys.files.item(nodeId) }).catch(() => undefined);
  }, [queryClient, nodeId]);

  // A machine saving the file raises a frame per save; those reads go through
  // the throttle the client shares (the event bridge asks it for the same
  // item), and a live document's own write-back fetches nothing while this tab
  // co-edits it. A person's rename, share or trash is read at once.
  useFrames(
    (frame) => namesNode(frame, nodeId, leaseNodeId),
    (frame) => {
      if (!isMachineRate(frame)) refresh();
      else if (frame.type === "file_node.changed") refreshForSave(queryClient, frame);
      else machineRefresh(queryClient).request(keys.files.item(nodeId));
    },
  );

  const item = query.data;
  const missing = query.error instanceof ApiError && query.error.status === 404;
  return {
    item,
    etag: item?.etag ?? null,
    ctag: item?.ctag || null,
    live: (item?.live as LiveFacet | undefined) ?? null,
    gone: missing || item?.trashed === true,
    refresh,
  };
}

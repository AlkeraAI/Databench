/**
 * Drag and drop for a Files listing, as its own hook.
 *
 * Two kinds of drag land on the same surfaces — rows the listing itself picked
 * up (a move) and files from the desktop (an upload) — and telling them apart
 * has to happen in ONE place, or a drop on a row also falls through to the
 * listing behind it. That one place is here, so every surface that mounts
 * `FilesBrowser` gets the same verdicts rather than a second copy of them.
 *
 * Nothing in here writes on its own: a move is handed to the actions api, which
 * owns the mutation, the report and the refusal copy, and an upload is handed
 * to the upload session.
 */

import { useCallback, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { api, request } from "@/api/client";
import { keys } from "@/api/keys";
import type { Item } from "@/api/files";
import { displayNameOf } from "@/lib/files/columns";
import {
  isFilesDrag,
  isKnownDrag,
  leftElement,
  moveVerdict,
  type MoveTargetNode,
} from "./dragDrop";
import {
  MOVE_MIME,
  readMovePayload,
  writeMovePayload,
  type DropEntry,
  type DropItem,
  type DropTarget,
} from "./dropHandlers";
import type { BrowserDragDrop } from "./FilesBrowser";
import type { FilesActionsApi } from "./FilesActions";
import { refusesWebWrites } from "./liveRoot/liveness";
import type { DropTransfer } from "./useUploads";

/** A browser `DataTransfer` read as the narrow surface the upload session takes.
 *
 *  Everything is read NOW, inside the drop event: `items` is a live
 *  `DataTransferItemList` the browser empties once the handler returns, and an
 *  entry asked for after that is `null`. Copying the entries out is what lets
 *  the target be resolved — sometimes by a read — before the upload starts. */
function asDropTransfer(transfer: DataTransfer): DropTransfer {
  const items: DropItem[] = Array.from(transfer.items).map((item) => {
    const entry = (item.webkitGetAsEntry?.() ?? null) as DropEntry | null;
    const file = entry === null && item.kind === "file" ? item.getAsFile() : null;
    return { kind: item.kind, webkitGetAsEntry: () => entry, getAsFile: () => file };
  });
  const moving = transfer.getData(MOVE_MIME);
  return {
    items,
    getData: (type: string) => (type === MOVE_MIME ? moving : transfer.getData(type)),
  };
}

/** A node as the move verdict reads it: the same shape a drop target has, plus
 *  the path that tells a subtree from a sibling. */
function moveTargetOf(item: Item | undefined, leased: boolean): MoveTargetNode | null {
  const target = dropTargetOf(item, leased);
  return target && item ? { ...target, path: item.path ?? null } : null;
}

/** A node as a drop target: the little the upload session needs to decide where
 *  the bytes land and whether they may. `leased` marks a folder a machine is
 *  holding on a surface that refuses writes into one; a surface that takes
 *  them (the chat's own Files tab) leaves it unset. */
export function dropTargetOf(item: Item | undefined, leased = false): DropTarget | null {
  if (!item) return null;
  return {
    id: item.id,
    name: item.nameDisplay || item.name,
    kind: item.kind,
    capabilities: { can_write: item.capabilities?.can_write ?? false },
    ...(leased ? { leased: true } : {}),
  };
}

/** The upload session, cut to the one call a drop makes. */
export interface DragDropUploads {
  onDrop: (target: DropTarget | null, transfer: DropTransfer) => Promise<void>;
}

export interface FilesDragDropOptions {
  /** The drive the rows live in. Until it is known, an unlisted target cannot
   *  be read and a drop on one lands nowhere. */
  driveId: string | undefined;
  /** The folder on screen, once its read has answered. */
  folder: Item | undefined;
  /** The id the URL carries, for the drop that arrives before that read does. */
  folderId?: string | undefined;
  /** How the folder is named in a sentence about it. A chat's working
   *  directory is named after the conversation, not after the directory. */
  displayAs?: Item | undefined;
  /** Whether a write may land in the folder on screen. Not `can_write` alone:
   *  the containers the drive creates homes and team folders in refuse every
   *  direct write, including an org admin's. */
  canWriteHere: boolean;
  /** Refuse every drop into, and every move out of, a folder a machine is
   *  holding under a lease. The Files page sets it: there a leased folder is
   *  the saved copy, read-only for the whole lease. The chat's own Files tab
   *  leaves it unset, because a write there is meant to land on the machine.
   *  Read off each target's own facet at the moment of the drop, so a row that
   *  is itself a leased folder, and a trail segment inside one, answer alike. */
  refuseHeld?: boolean;
  /** The rows on screen, so a target that is one of them needs no read. */
  rows: readonly Item[];
  /** The rows selected right now: a drag started on one of them carries them all. */
  selection: readonly Item[];
  uploads: DragDropUploads;
  /** The rows a drag that started on a row is carrying, for the life of the drag.
   *  A drag-over can read only the transfer's TYPES, never its payload, so the
   *  rows are remembered here and the payload is read back only on the drop. */
  dragging: React.MutableRefObject<readonly Item[]>;
}

/** What the listing element spreads to become a drop target of its own. */
export interface ListingDropProps {
  "data-drop-active": "true" | undefined;
  onDragEnter: (event: React.DragEvent<HTMLElement>) => void;
  onDragOver: (event: React.DragEvent<HTMLElement>) => void;
  onDragLeave: (event: React.DragEvent<HTMLElement>) => void;
  onDrop: (event: React.DragEvent<HTMLElement>) => void;
}

export interface FilesDragDrop {
  /** Every drop the surface accepts: on a folder row, on a segment of the
   *  trail, or on the listing itself. */
  dropOn: (targetId: string, event: React.DragEvent<HTMLElement>, acts: FilesActionsApi) => void;
  /** What `FilesBrowser` needs to make its rows draggable and droppable. */
  dragDropFor: (acts: FilesActionsApi) => BrowserDragDrop;
  /** What the element around the listing spreads to accept a drop of its own. */
  listingProps: (acts: FilesActionsApi) => ListingDropProps;
  /** Whether a drag is over the listing right now, for the hint drawn over it. */
  listingDropActive: boolean;
  /** What the folder on screen is called, for a sentence about it. */
  hereName: string;
}

export function useFilesDragDrop(options: FilesDragDropOptions): FilesDragDrop {
  const {
    driveId,
    folder,
    folderId,
    displayAs,
    canWriteHere,
    refuseHeld = false,
    rows,
    selection,
    uploads,
    dragging,
  } = options;
  const qc = useQueryClient();
  // A desktop drag is over the listing: the listing as a whole is the target
  // (the folder on screen) until a row or a segment under the pointer takes over.
  const [listingDrop, setListingDrop] = useState(false);

  const rowsById = useMemo(() => new Map(rows.map((row) => [row.id, row])), [rows]);

  const hereName = displayAs
    ? displayNameOf(displayAs)
    : folder
      ? displayNameOf(folder)
      : "this folder";

  /** The node a drop names, as the surface knows it: a row of the listing, the
   *  folder on screen, or an ancestor on the trail. The trail's segments carry
   *  only an id and a name, so an ancestor's capabilities come from the cache
   *  the walk down filled — and from one read when the cache has moved on. A
   *  read is not a write: nothing is moved or uploaded before the answer. */
  const resolveTarget = useCallback(
    async (id: string): Promise<Item | undefined> => {
      const listed = rowsById.get(id);
      if (listed) return listed;
      if (folder?.id === id) return folder;
      const cached = qc.getQueryData<Item>(keys.files.item(id));
      if (cached) return cached;
      if (driveId === undefined || id === "") return undefined;
      try {
        return await qc.fetchQuery<Item>({
          queryKey: keys.files.item(id),
          queryFn: () =>
            request(
              api.GET("/api/v1/files/drives/{drive_id}/items/{item_id}", {
                params: { path: { drive_id: driveId, item_id: id } },
              }),
            ),
        });
      } catch {
        return undefined;
      }
    },
    [rowsById, folder, qc, driveId],
  );

  const dropOn = (targetId: string, event: React.DragEvent<HTMLElement>, acts: FilesActionsApi) => {
    event.preventDefault();
    event.stopPropagation();
    setListingDrop(false);
    const transfer = asDropTransfer(event.dataTransfer);
    const moving = readMovePayload(transfer);
    const carried = dragging.current;
    dragging.current = [];
    // The lease is judged at the instant of the drop, off the facets the
    // target and the listing carry: a drop is an event, not a render.
    const now = Date.now();
    void resolveTarget(targetId).then((target) => {
      const targetHeld = refuseHeld && refusesWebWrites(target, now);
      if (moving) {
        // Only rows this surface picked up are moved: they are the ones whose
        // parent and path the verdict can read. A payload from elsewhere (another
        // tab) names rows this listing knows nothing about, and does nothing.
        const subjects = carried.filter((row) => moving.some((subject) => subject.id === row.id));
        if (subjects.length === 0) return;
        const decision = moveVerdict(subjects, moveTargetOf(target, targetHeld), {
          name: hereName,
          canWrite: canWriteHere,
          leased: refuseHeld && refusesWebWrites(folder, now),
        });
        if (decision.refusal !== null) {
          acts.refuse(decision.refusal);
          return;
        }
        if (decision.moves.length > 0) acts.moveInto(decision.moves, targetId, target);
        return;
      }
      void uploads.onDrop(dropTargetOf(target, targetHeld), transfer);
    });
  };

  const dragDropFor = (acts: FilesActionsApi): BrowserDragDrop => ({
    onDragStart: (item, event) => {
      // A row inside the selection drags the whole selection; a row outside it
      // drags alone, and the selection is left as it was.
      const carried = selection.some((row) => row.id === item.id) ? selection : [item];
      dragging.current = carried;
      writeMovePayload(
        event.dataTransfer,
        carried.map((row) => ({ id: row.id, etag: row.etag })),
      );
      event.dataTransfer.effectAllowed = "move";
    },
    onDragEnd: () => {
      dragging.current = [];
    },
    // A folder may be landed on unless it is one of the rows in the air: a
    // folder cannot be dropped into itself, and the ring must not say it can.
    droppable: (item) =>
      item.kind === "folder" && !dragging.current.some((row) => row.id === item.id),
    onDropOnRow: (item, event) => dropOn(item.id, event, acts),
  });

  const listingProps = (acts: FilesActionsApi): ListingDropProps => ({
    "data-drop-active": listingDrop ? "true" : undefined,
    onDragEnter: (event) => {
      if (isFilesDrag(event.dataTransfer)) setListingDrop(true);
    },
    onDragOver: (event) => {
      // Only a drag the surface knows how to land is allowed to land:
      // dragged text or a link keeps the browser's own refusal.
      if (!isKnownDrag(event.dataTransfer)) return;
      event.preventDefault();
      if (isFilesDrag(event.dataTransfer) && !listingDrop) setListingDrop(true);
    },
    onDragLeave: (event) => {
      if (leftElement(event)) setListingDrop(false);
    },
    onDrop: (event) => dropOn(folder?.id ?? folderId ?? "", event, acts),
  });

  return { dropOn, dragDropFor, listingProps, listingDropActive: listingDrop, hereName };
}

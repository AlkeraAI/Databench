/**
 * "Move to…" — a folder picker over the same tree the browser lists.
 *
 * It is the browser's own parts in a dialog: the breadcrumb trail, the treegrid
 * in its list layout (folders only, read-only), and the shared Modal for the
 * chrome — not a second, flatter rendering of the same folders. Walking it walks
 * the real listing, so a folder the caller cannot write to is refused by the same
 * capability the browser shows, and the destination that comes back is a node id
 * the move route already accepts.
 *
 * A single click picks a folder; opening one (double-click or Enter) descends
 * into it; the primary action lands in the picked folder, or the one being
 * listed when nothing is picked — the way every desktop picker reads. A search
 * field finds a folder anywhere in the drive by name: the grid then lists the
 * matches with where each one lives, and picking or opening one works the same.
 *
 * A node that IS something else — a promoted result, say — is not a place, so
 * the picker neither lists one nor walks into one. A CHAT and a CHAT TEMPLATE
 * are the exceptions: both are real folders that hold working files — the one
 * its agent runs in, the one a new chat starts with — so moving a file into
 * either is how a person hands it over. They are listed, picked and walked into
 * like any folder; the server decides where inside one the file lands and the
 * answer names that folder back.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Modal, TextInput } from "@alkera/ui";

import {
  flattenChildren,
  isObjectBacked,
  useChildren,
  useDrive,
  useItem,
  type Item,
  type OrderBy,
} from "@/api/files";
import { isPathBelow } from "@/lib/paths";

import { Breadcrumbs, displayPlaceName, type Crumb } from "./Breadcrumbs";
import { isFolderObject } from "@/lib/files/chatFolder";
import { displayNameOf, displayPath, isHome } from "@/lib/files/columns";
import { Treegrid } from "./Treegrid";
import { emptySelection, selectionReducer, type SelectionAction } from "./state/selection";
import type { Platform } from "./state/shortcuts";
import { enclosingPath, useSearchQuery } from "./useSearchQuery";
import { acceptsWrites } from "./writeAccess";
// The picker owns its sheet: a chat's file tab and files tab open it (Move to…,
// Copy to…) from chunks that never load the files browser's stylesheet.
import "./move-to-dialog.css";

export interface MoveToDialogProps {
  open: boolean;
  driveId: string | undefined;
  /** Where the picker opens — normally the folder being listed. */
  startFolderId: string | undefined;
  startLabel?: string;
  /** The ancestors of that folder, outermost first and the folder itself last, so
   *  the trail climbs instead of standing on one crumb. Left out, the picker still
   *  roots itself at the drive. */
  startTrail?: readonly Crumb[];
  /** The rows the caller is about to move or copy. A destination inside one of
   *  them is refused here rather than by a 409 a moment later. */
  moving?: readonly Item[];
  /** How many items are being moved, for the default title. */
  count: number;
  /** The heading. Defaults to "Move N item(s) to…". */
  title?: string;
  /** The primary action. Defaults to "Move here". */
  confirmLabel?: string;
  platform?: Platform;
  /** Test seam for the treegrid's virtualizer (jsdom measures nothing). */
  initialRect?: { width: number; height: number };
  onCancel: () => void;
  /** The destination, the row it came from when the picker had one, and the rows
   *  that can actually land there. The row rides along so the caller can name the
   *  place in its own copy without a second read; it is absent when the
   *  destination is the folder being listed. `movable` is a subset of `moving` —
   *  the selection minus the rows this destination cannot take — and is absent
   *  when the caller named no rows. */
  onConfirm: (parentId: string, destination?: Item, movable?: readonly Item[]) => void;
}

/** Folders read best by name; the picker never re-asks the server for another order. */
const BY_NAME: OrderBy = { field: "name", direction: "asc" };

/** What the drive root reads as in the trail, in both pickers. */
export const DRIVE_ROOT_LABEL = "My drive";

function noSort(): void {
  /* a picker lists folders by name; the header reports it and does not re-ask */
}

/**
 * The trail the picker opens on: the ancestors the caller walked, rooted at the
 * drive.
 *
 * A picker that only walked down left the commonest move there is — one level
 * up — reachable only by typing the destination's name, and a one-character
 * name not reachable at all.
 */
export function pickerTrail(seed: {
  homeId?: string | undefined;
  startTrail?: readonly Crumb[] | undefined;
  startFolderId?: string | undefined;
  startLabel: string;
}): Crumb[] {
  const walked =
    seed.startTrail && seed.startTrail.length > 0
      ? [...seed.startTrail]
      : seed.startFolderId === undefined
        ? []
        : [{ id: seed.startFolderId, name: displayPlaceName(seed.startLabel) }];
  const home = seed.homeId;
  if (home === undefined || walked.some((crumb) => crumb.id === home)) return walked;
  return [{ id: home, name: DRIVE_ROOT_LABEL }, ...walked];
}

/**
 * Whether the destination sits inside this row.
 *
 * Two proofs, in order: the paths, when both are known, and otherwise nothing —
 * which is the answer for a destination reached by SEARCH, where the trail the
 * picker walked says nothing about where the hit lives. An unprovable
 * destination is treated as inside, because the cost of the two answers is not
 * symmetric: refusing a legal folder costs a second click, offering an illegal
 * one costs a 409 the person then has to read.
 */
function isUnderMoved(destination: Item | undefined, row: Item, viaSearch: boolean): boolean {
  if (row.kind !== "folder") return false;
  const where = destination ? displayPath(destination) : undefined;
  const root = displayPath(row);
  // Only strictly inside: the folder itself is caught by its id, and two rows
  // can carry the same path only when they are the same row.
  if (where && root) return isPathBelow(root, where);
  return viaSearch;
}

export interface DestinationCheck {
  /** The folder the action would land in. */
  destinationId: string | undefined;
  /** The row for that folder — the one picked, or the one walked into. Absent
   *  for a folder the picker only has a crumb for. */
  destination?: Item | undefined;
  /** Every folder the picker is standing inside, outermost first. */
  trailIds: readonly string[];
  /** The rows being moved or copied. */
  moving: readonly Item[];
  /** The destination was reached by a search, so the trail is no proof of where
   *  it sits and only a path can be. */
  viaSearch?: boolean;
  /** The row for the destination has been asked for and has not arrived. Nothing
   *  is offered on a folder whose permissions are not known yet — but nothing is
   *  claimed about it either. */
  awaiting?: boolean;
}

export interface DestinationVerdict {
  /** The one sentence to show about this destination, or nothing to say. */
  reason: string | null;
  /** The rows that can still land here. */
  movable: readonly Item[];
  /** Nothing can land here, so the action is not offered. */
  blocksAll: boolean;
}

/**
 * What this destination will accept, and the sentence that says so.
 *
 * The client knows what it is moving and where it is standing, so it knows the
 * answer the server is going to give: offering the destination anyway spends a
 * request to tell the person what could have been said before they clicked.
 *
 * The verdict is per ROW, because a selection is not one thing: one folder that
 * cannot go inside itself must not hold back the four files selected with it.
 * Only a refusal the whole destination earns — the trash, no write — stops
 * everything.
 */
export function destinationVerdict(check: DestinationCheck): DestinationVerdict {
  const { destinationId, destination, trailIds, moving, viaSearch = false, awaiting } = check;
  if (destinationId === undefined) return { reason: null, movable: [], blocksAll: true };
  // The row is on its way. The action waits for it rather than being offered on a
  // folder nothing is known about, and says nothing it would have to take back.
  if (awaiting === true) return { reason: null, movable: [], blocksAll: true };
  if (destination?.trashed === true) {
    return { reason: "A folder in the trash can't hold items.", movable: [], blocksAll: true };
  }
  // Fail closed, and for every folder the picker stands in — the one it opened on
  // and the ancestor crumbs included, not only a row that was clicked.
  if (!acceptsWrites(destination)) {
    return { reason: "You can't add items to this folder.", movable: [], blocksAll: true };
  }
  if (moving.length === 0) return { reason: null, movable: [], blocksAll: false };

  const cycles = moving.filter(
    (row) =>
      row.id === destinationId ||
      trailIds.includes(row.id) ||
      isUnderMoved(destination, row, viaSearch),
  );
  const settled = moving.filter((row) => !cycles.includes(row) && row.parentId === destinationId);
  const movable = moving.filter((row) => !cycles.includes(row) && !settled.includes(row));
  if (movable.length === 0) {
    if (cycles.length > 0) {
      return { reason: "A folder can't move inside itself.", movable, blocksAll: true };
    }
    return {
      reason: moving.length === 1 ? "This item is already here." : "These items are already here.",
      movable,
      blocksAll: true,
    };
  }
  const [firstCycle] = cycles;
  const [firstSettled] = settled;
  if (cycles.length + settled.length === 0) return { reason: null, movable, blocksAll: false };
  if (cycles.length === 1 && firstCycle && settled.length === 0) {
    return { reason: `${displayNameOf(firstCycle)} can't move inside itself.`, movable, blocksAll: false };
  }
  if (settled.length === 1 && firstSettled && cycles.length === 0) {
    return { reason: `${displayNameOf(firstSettled)} is already here.`, movable, blocksAll: false };
  }
  return {
    reason: `${cycles.length + settled.length} items are staying where they are.`,
    movable,
    blocksAll: false,
  };
}

export function MoveToDialog({
  open,
  driveId,
  startFolderId,
  startLabel = "Files",
  startTrail,
  moving,
  count,
  title,
  confirmLabel = "Move here",
  platform,
  initialRect,
  onCancel,
  onConfirm,
}: MoveToDialogProps) {
  const [trail, setTrail] = useState<readonly Crumb[]>([]);
  // The row for each folder the picker has walked into. A crumb carries a name
  // and an id; whether the folder will take a write is on the ROW the listing
  // handed over, and walking down is how this picker is mostly used.
  const [walkedRows, setWalkedRows] = useState<Record<string, Item>>({});
  // Set once the trail has been restarted at a search hit: from then on the trail
  // is no proof of where the folder on screen sits in the tree.
  const [viaSearch, setViaSearch] = useState(false);
  const [selection, setSelection] = useState(emptySelection);
  const [query, setQuery] = useState("");
  // The drive read every Files surface already holds: it names the home folder,
  // which is the one ancestor a picker can always offer.
  const drive = useDrive({ enabled: open });
  const homeId = drive.data?.homeId ?? undefined;

  // Set once the person has moved in the picker: the drive read can land after
  // the dialog opened, and a seed that ran then would pull them back up.
  const walked = useRef(false);

  // Reopening the picker starts at the folder on screen, never where it was left.
  // `startTrail` is the caller's own trail and is read by identity: a caller that
  // builds a new array every render would re-seed the picker under the person.
  useEffect(() => {
    if (!open) {
      walked.current = false;
      return;
    }
    if (walked.current) return;
    setTrail(pickerTrail({ homeId, startTrail, startFolderId, startLabel }));
    setWalkedRows({});
    setViaSearch(false);
    setSelection(emptySelection);
    setQuery("");
  }, [open, homeId, startTrail, startFolderId, startLabel]);

  const here = trail.length > 0 ? trail[trail.length - 1] : undefined;
  // A closed picker has no trail, so `here` is undefined and the listing never runs.
  const children = useChildren(driveId, here?.id, { filters: { kind: "folder" } });
  // The search reads the whole drive, folders only, as the toolbar's search does;
  // it is idle until the field holds enough to ask with.
  const search = useSearchQuery({
    driveId: open ? driveId : undefined,
    folderId: undefined,
    scope: "drive",
    text: query,
    filters: { kind: "folder" },
  });
  const searching = search.isActive;
  // The one list the picker reads: what it lists, what it walks into, and what it
  // hands back as the destination all come from here. The kind is not enough on its
  // own — a chat is stored as a folder — so an object-backed node is dropped unless
  // it is a chat or a chat template: those are places, because their files are the
  // working material of a conversation, and a file moved into one is a file that
  // conversation can read.
  const folders = useMemo(
    () =>
      (searching ? search.items : flattenChildren(children.data)).filter(
        (row: Item) => row.kind === "folder" && (!isObjectBacked(row) || isFolderObject(row)),
      ),
    [searching, search.items, children.data],
  );

  const onSelectionAction = useCallback(
    (action: SelectionAction) => {
      setSelection((current) =>
        selectionReducer(
          current,
          action,
          folders.map((row) => row.id),
        ),
      );
    },
    [folders],
  );

  const descend = useCallback(
    (folder: Item) => {
      if (folder.kind !== "folder") return;
      const crumb = { id: folder.id, name: displayNameOf(folder), ...(isHome(folder) ? { home: true } : {}) };
      // Out of a search the folder was reached from anywhere, so the trail starts
      // over at it — the same rule the browser's own trail follows.
      setTrail((current) => (searching ? [crumb] : [...current, crumb]));
      setWalkedRows((current) => ({ ...current, [folder.id]: folder }));
      if (searching) setViaSearch(true);
      setSelection(emptySelection);
      setQuery("");
      walked.current = true;
    },
    [searching],
  );

  const backTo = useCallback((id: string) => {
    walked.current = true;
    setTrail((current) => {
      const at = current.findIndex((crumb) => crumb.id === id);
      return at >= 0 ? current.slice(0, at + 1) : current;
    });
    setSelection(emptySelection);
  }, []);

  // The picked folder when exactly one is picked and still listed, else the folder open.
  const picked = useMemo(() => {
    if (selection.selected.size !== 1) return undefined;
    const [id] = selection.selected;
    return folders.find((row) => row.id === id);
  }, [selection, folders]);
  // While searching there is no "folder open" to fall back on: a match has to be picked.
  const destination = picked?.id ?? (searching ? undefined : here?.id);
  // The row behind that id: the one picked, or the one walked into. A folder the
  // picker only holds a crumb for — an ancestor the caller handed it — has none.
  const walkedHere = here ? walkedRows[here.id] : undefined;
  // The picker also stands in folders it never walked into: the one it opened on,
  // and any ancestor crumb the caller handed it. Those are read, because a folder
  // whose permissions are unknown is one the picker must not offer. The read is
  // the page's own cache entry for that node, so it is usually already there.
  const standing = useItem(
    driveId,
    open && walkedHere === undefined && !searching ? here?.id : undefined,
  );
  const destinationRow = picked ?? walkedHere ?? standing.data;
  // Asked for and not answered: not a refusal, not an offer.
  const awaiting = destination !== undefined && destinationRow === undefined && !standing.isError;

  // What this destination will take, and the sentence that says so: decided here,
  // before the click, rather than by the server a moment after it.
  const verdict = useMemo(
    () =>
      destinationVerdict({
        destinationId: destination,
        destination: destinationRow,
        trailIds: trail.map((crumb) => crumb.id),
        moving: moving ?? [],
        viaSearch: viaSearch || (searching && picked !== undefined),
        awaiting,
      }),
    [destination, destinationRow, trail, moving, viaSearch, searching, picked, awaiting],
  );
  const refusal = verdict.reason;

  // A search result needs where it lives: two folders can share a name.
  const nameWithPlace = useCallback(
    (row: Item) =>
      searching ? (
        <span className="alk-files-picker__name">
          <span>{displayNameOf(row)}</span>
          <span className="alk-files-picker__where">{enclosingPath(row)}</span>
        </span>
      ) : null,
    [searching],
  );

  const heading = title ?? (count === 1 ? "Move 1 item to…" : `Move ${count} items to…`);

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={heading}
      size="lg"
      className="alk-files-picker"
      confirmLabel={confirmLabel}
      confirmDisabled={destination === undefined || verdict.blocksAll}
      onConfirm={() => {
        if (destination === undefined || verdict.blocksAll) return;
        onConfirm(destination, picked, moving === undefined ? undefined : verdict.movable);
      }}
      footerDivided
    >
      <TextInput
        type="search"
        size="md"
        rootClassName="alk-files-picker__search"
        autoComplete="off"
        spellCheck={false}
        aria-label="Find a folder"
        placeholder="Find a folder"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          // Escape empties the field before it can close the dialog.
          if (event.key === "Escape" && query !== "") {
            event.stopPropagation();
            setQuery("");
          }
        }}
      />
      {/* The trail stays put while searching: a line that swapped in above the grid moved
          everything under it on every keystroke. The grid says when nothing matches. */}
      <div className="alk-files-picker__trail">
        <Breadcrumbs segments={trail} onNavigate={backTo} label="Destination" />
      </div>
      <div className="alk-files-picker__grid">
        {(searching ? search.isLoading : children.isPending && here !== undefined) ? (
          <p className="alk-files-picker__empty" aria-busy="true">
            {searching ? "Searching…" : "Loading folders…"}
          </p>
        ) : folders.length === 0 ? (
          <p className="alk-files-picker__empty">
            {searching ? `No folders match “${search.settledText}”.` : "No folders here."}
          </p>
        ) : (
          <Treegrid
            rows={folders}
            view="list"
            selection={selection}
            onSelectionAction={onSelectionAction}
            orderBy={BY_NAME}
            onSort={noSort}
            onOpen={descend}
            renderNameOverride={nameWithPlace}
            platform={platform}
            label="Folders"
            {...(initialRect ? { initialRect } : {})}
          />
        )}
      </div>
      {refusal === null ? null : (
        <p className="alk-files-picker__refusal" role="status">
          {refusal}
        </p>
      )}
    </Modal>
  );
}

export default MoveToDialog;

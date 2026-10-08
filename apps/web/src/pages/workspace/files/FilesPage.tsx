import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Navigate, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { nameNote, validateName } from "@alkera/chat-model";
import { SplitPane } from "@alkera/ui";
import { useCurrentUser } from "@/api/auth";
import { userScope } from "@/lib/accountScope";
import { usePersistedSize, type SizeBounds } from "@/app/usePersistedSize";
import {
  flattenChildren,
  useChildren,
  useCreateFolder,
  useDrive,
  useItem,
  useMintContentGrant,
  useRecent,
  useSharedWithMe,
  type Item,
  type MintContentGrant,
  type OrderDirection,
  type OrderField,
} from "@/api/files";
import { Breadcrumbs } from "./Breadcrumbs";
import { displayNameOf } from "@/lib/files/columns";
import {
  isChatFolder,
  isFolderObject,
  isTemplateFolder,
  TEMPLATE_FILES_NOTICE,
} from "@/lib/files/chatFolder";
import { browseTargetOf, openTargetOf, pageDoorOf, secondPageDoorOf } from "@/lib/files/openTarget";
import { chatRecordsRule, useShowHiddenFiles, visibleRows } from "./hiddenEntries";
import { LEASED_HERE, type MenuActionId } from "./contextMenuItems";
import { contentUrl, downloadItem } from "@/lib/files/download";
import { FilesActions, type FilesActionsApi } from "./FilesActions";
import { FilesBrowser } from "./FilesBrowser";
// The toolbar shows no filter chips; the empty state that names a filter still
// renders for a link that carries one.
import { FilteredEmptyState } from "./FilterBar";
import { LeaseBadge } from "./LeaseBadge";
import { MyLeases } from "./MyLeases";
import { LiveBadge } from "./live/LiveBadge";
import { LiveRowChip } from "./live/LiveRowChip";
import { countOnBox, useFolderLiveness } from "./live/useFolderLiveness";
import { chatLeaseOf, isHeldByMachine, refusesWebWrites } from "./liveRoot/liveness";
import { canLease, useNow } from "./useLeaseFacet";
import { RenameInline } from "./RenameInline";
import { MoveToDialog } from "./MoveToDialog";
import { RightPane } from "./RightPane";
import { SearchBar, SearchResults } from "./SearchBar";
import { SelectionBar } from "./SelectionBar";
import { Sidebar, type FilesPlaceId } from "./Sidebar";
import { Treegrid } from "./Treegrid";
import { emptySelection, selectionReducer, type SelectionAction } from "./state/selection";
import { ThresholdFooter } from "./ThresholdFooter";
import { TrashPage } from "./TrashPage";
import { UndoToast } from "./UndoToast";
import { UploadTray } from "./UploadTray";
import { filesErrorCopy, type FilesErrorCopy } from "@/lib/files/errors";
import { landingState } from "./landing";
import { NotHere } from "./NotHere";
import { SoloItem } from "./SoloItem";
import { hasActiveFilters, toListFilters, useFilterState } from "./filterState";
import { type DropEntry, type DropItem } from "./dropHandlers";
import { detectPlatform, type Platform } from "@/lib/platform";
import { displayPlaceName } from "./Breadcrumbs";
import { useCrumbTrail } from "./useCrumbTrail";
import { dropTargetOf, useFilesDragDrop } from "./useFilesDragDrop";
import { useSearchQuery } from "./useSearchQuery";
import { FilePreviewModal } from "./preview/FilePreviewModal";
import { folderResolver } from "./preview/folderResolver";
import { isPreviewable, previewKindOf, useLinkedSelection } from "./preview/useLinkedSelection";
import { viewableUrl } from "./viewer";
import { useSoftThreshold } from "./useSoftThreshold";
import { undoStep, useUndoStack } from "./undo";
import { useUploads, type DropTransfer } from "./useUploads";
import { keys } from "@/api/keys";
import { notebookEditorQuery, useNotebookEditor } from "@/api/notebooks";
import { isNotebookName } from "@/lib/files/fileTypes";
import { DEFAULT_NOTEBOOK_NAME } from "../chat/workspace/notebook/notebookNames";
import { chatFileHref } from "../chat/openFileLink";
import "./files-page.css";

export { objectRoute } from "@/lib/files/objectRoute";

/** The address a preview carries: which row, and what kind of preview it opens.
 *  Named once so the page that writes them and any link that reads them cannot
 *  disagree about the spelling. */
export const PREVIEW_PARAM = "preview";
export const PREVIEW_KIND_PARAM = "kind";

/** The feeds: drive-scoped listings of their own, with no node to route to.
 *  Each is answered by its own route, so it needs the drive and nothing else. */
export const FEED_PLACES = ["recent", "sharedWithMe"] as const;

export type FeedPlaceId = (typeof FEED_PLACES)[number];

export function isFeedPlace(place: FilesPlaceId | undefined): place is FeedPlaceId {
  return place !== undefined && (FEED_PLACES as readonly FilesPlaceId[]).includes(place);
}

/** The feed a `?place=` asks for, or nothing.
 *
 *  The parameter arrives from a link anybody can write, so it is matched
 *  against the two feeds by name and never used as a lookup key: an unknown
 *  value — a retired place, a path, a script — leaves the page on the listing
 *  it would have shown anyway rather than becoming part of a request. */
export function feedPlaceFrom(raw: string | null | undefined): FeedPlaceId | undefined {
  return FEED_PLACES.find((place) => place === raw);
}

/** Rail places that are a view over the same drive rather than a node to route
 *  to: the two feeds, and the leases this person holds. Each is answered by a
 *  drive-scoped route of its own, so all a link needs to carry is its name. */
const VIRTUAL_PLACE_IDS = ["leases", ...FEED_PLACES] as const;

export type VirtualPlaceId = (typeof VIRTUAL_PLACE_IDS)[number];

export const VIRTUAL_PLACES: readonly FilesPlaceId[] = VIRTUAL_PLACE_IDS;

/** The virtual place a `?place=` asks for, or nothing. Matched by name against
 *  the list above for the same reason {@link feedPlaceFrom} is: the value comes
 *  from a link anybody can write and must never become part of a request. */
export function virtualPlaceFrom(raw: string | null | undefined): VirtualPlaceId | undefined {
  return VIRTUAL_PLACE_IDS.find((place) => place === raw);
}

/** Which rail entry the page is standing on, or none.
 *
 *  Derived from where the browser IS, never from what was last clicked. Two
 *  things follow, and both were wrong while the rail read a click:
 *
 *  - Landing on Home marks Home. `/files` redirects to the home node without
 *    anyone touching the rail, so the place a person reads first was the one
 *    entry the rail never lit.
 *  - A folder INSIDE a place marks nothing. The rail names places, not the
 *    route that reached them, so a descendant of Home is not Home.
 *
 *  A node-addressed place is therefore matched on identity — the node on screen
 *  IS that place's node — while a feed and Trash are views with no node of their
 *  own and are named by the view the page is rendering. */
export function currentPlace(options: {
  trash: boolean;
  nodeId: string | undefined;
  place: FilesPlaceId | undefined;
  placeNodeIds: Partial<Record<FilesPlaceId, string>> | undefined;
}): FilesPlaceId | undefined {
  if (options.trash) return "trash";
  // A feed replaces the listing, so it owns the rail regardless of the node in the URL.
  if (isFeedPlace(options.place)) return options.place;
  if (options.nodeId === undefined || options.nodeId === "") return undefined;
  for (const [id, target] of Object.entries(options.placeNodeIds ?? {})) {
    if (target === options.nodeId) return id as FilesPlaceId;
  }
  return undefined;
}

/** What Cmd+Up answers when there is no folder above this one to open — the
 *  drive's root, or a file shared without the folder it lives in. A key that
 *  silently did nothing was read as a broken keyboard rather than as an answer. */
export const NO_PARENT_HERE = "You can't open the folder above this one.";

/** The bar's create and upload buttons, as action ids the context menu dispatches
 *  too. Without them these exist only as a right-click and a keystroke,
 *  which a touch device and a keyboard-only user have no route to. */
export const CREATE_ACTIONS: readonly (readonly [MenuActionId, string])[] = [
  ["new-folder", "New folder"],
  ["new-notebook", "New notebook"],
  ["upload-files", "Upload files"],
  ["upload-folder", "Upload folder"],
];

/** The two traversal-only containers listed directly under the drive root.
 *
 *  Spelled the way the server spells them, because the name is all the listing
 *  carries; `Shared` is a real folder and is deliberately absent. */
const SIGNPOST_NAMES: readonly string[] = ["home", "Teams"];

/** Whether the folder on screen is a signpost rather than a place files live.
 *
 *  `/`, `home/` and `Teams/` hold exactly the homes and team folders the server
 *  creates for a member or a team, each born with the grant that makes it
 *  reachable. A node written there by hand would carry none, so every direct
 *  write is refused (`files.container_readonly`) — for an org admin too, who is
 *  otherwise a writer there by descent and so is offered the buttons by
 *  `canWrite` alone. The page must not offer a control whose only outcome is
 *  that refusal. */
export function isSignpost(
  folder: { readonly id?: string; readonly parentId?: string | null; readonly name?: string },
  rootId: string | undefined,
): boolean {
  if (rootId === undefined || rootId === "") return false;
  if (folder.id === rootId) return true;
  return folder.parentId === rootId && SIGNPOST_NAMES.includes(folder.name ?? "");
}

/** The Files page shell: the rail, the breadcrumb, the toolbar, the browser and the right pane.
 *
 *  The shell owns placement and nothing else. The browser, the toolbar, the breadcrumb trail and
 *  the pane arrive as children so the listing, the selection and the metadata read can be built
 *  and tested apart from the frame that holds them.
 *
 *  `/files` carries no node, so it is not a place — it is the request "open my files". Where that
 *  lands is `/home/<me>`, whose node id only the drive's root listing knows (the server resolves
 *  the username; the client never spells it). The listing supplies it through `FilesHomeProvider`,
 *  and the shell redirects the moment it has one. */

/** What the root listing has resolved about the caller's home folder.
 *  `undefined` — not resolved yet; `null` — the caller has no home folder in this drive. */
export interface FilesHome {
  nodeId: string | null | undefined;
}

const FilesHomeContext = createContext<FilesHome>({ nodeId: undefined });

/** Publishes the caller's home node to the shell. The listing owns the fetch; the shell only
 *  needs the id, so the redirect stays testable without a network in the frame. */
export function FilesHomeProvider({
  nodeId,
  children,
}: {
  nodeId: string | null | undefined;
  children: ReactNode;
}) {
  const value = useMemo(() => ({ nodeId }), [nodeId]);
  return <FilesHomeContext.Provider value={value}>{children}</FilesHomeContext.Provider>;
}

export function useFilesHome(): FilesHome {
  return useContext(FilesHomeContext);
}

/** The portal's narrow breakpoint (`styles/portal.css`). Below it the shell is one column and
 *  the right pane becomes a sheet over the browser rather than a third column. */
export const FILES_NARROW_QUERY = "(max-width: 900px)";

function useNarrow(): boolean {
  const [narrow, setNarrow] = useState(
    () => typeof window !== "undefined" && window.matchMedia?.(FILES_NARROW_QUERY).matches === true,
  );
  useEffect(() => {
    const query = window.matchMedia?.(FILES_NARROW_QUERY);
    if (!query) return;
    setNarrow(query.matches);
    const onChange = (event: MediaQueryListEvent) => setNarrow(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return narrow;
}

/** The rail that lists places. Narrow enough for the labels, wide enough that a
 *  long place name is not an ellipsis. */
export const FILES_RAIL_BOUNDS: SizeBounds = { min: 168, max: 400, size: 240 };

/** The details pane. Its ceiling is a share of the window rather than a number:
 *  a listing squeezed to nothing by a pane on a 1280px screen is PL-19's
 *  complaint, and the share is what stops it. */
export const FILES_PANE_BOUNDS: SizeBounds = { min: 260, max: 560, size: 320 };

/**
 * The three columns, at widths this browser remembers.
 *
 * One answer for the whole drive, not one per folder: the rail and the details
 * pane say the same kind of thing wherever the reader is, so a width set in one
 * folder is the width they meant everywhere. Both can be folded away — the
 * listing is the page, and on a narrower monitor it is the only column worth the
 * room.
 */
function FilesSplit({
  rail,
  listing,
  pane,
}: {
  rail: ReactNode;
  listing: ReactNode;
  pane: ReactNode;
}) {
  const [railSize, setRail] = usePersistedSize("files.rail", FILES_RAIL_BOUNDS);
  const [paneSize, setPane] = usePersistedSize("files.pane", FILES_PANE_BOUNDS);
  const panes = [
    {
      id: "rail",
      label: "Places",
      min: FILES_RAIL_BOUNDS.min,
      max: FILES_RAIL_BOUNDS.max,
      size: railSize.width,
      collapsible: true as const,
      collapsed: railSize.collapsed,
    },
    { id: "listing", label: "Files", fill: true as const, min: 320 },
    ...(pane
      ? [
          {
            id: "details",
            label: "Details",
            min: FILES_PANE_BOUNDS.min,
            max: "40%" as const,
            size: paneSize.width,
            collapsible: true as const,
            collapsed: paneSize.collapsed,
          },
        ]
      : []),
  ];
  const set = (id: string, part: { width?: number; collapsed?: boolean }): void => {
    if (id === "rail") setRail(part);
    else if (id === "details") setPane(part);
  };

  return (
    <SplitPane
      label="Files"
      panes={panes}
      onResize={(id, px) => set(id, { width: px })}
      onToggle={(id, collapsed) => set(id, { collapsed })}
    >
      {rail}
      {listing}
      {pane ? (
        <aside className="alk-files__pane" aria-label="Details">
          {pane}
        </aside>
      ) : null}
    </SplitPane>
  );
}

export interface FilesPageProps {
  /** The trash view (`/files/trash`) rather than a folder listing. */
  trash?: boolean;
  toolbar?: ReactNode;
  breadcrumb?: ReactNode;
  browser?: ReactNode;
  /** The details pane. Rendered as a column when there is room and as a sheet below the
   *  breakpoint; absent, the third column collapses instead of showing an empty frame. */
  pane?: ReactNode;
  /** Where a rail place addressed by node id goes, once the listing knows. */
  placeNodeIds?: Partial<Record<FilesPlaceId, string>>;
  /** Places the page answers itself rather than by routing to a node — they are reachable
   *  in the rail even though no node id will ever resolve for them. */
  virtualPlaces?: readonly FilesPlaceId[];
  /** True once the drive itself has loaded. A feed place is answered by a
   *  drive-scoped route, so this is all it waits for — without it the rail
   *  would keep the three feeds greyed out on a perfectly live drive. */
  driveReady?: boolean;
  /** True when opening the drive has failed for good — the drive read refused,
   *  the drive named no root node, or the root listing refused. `/files` has no
   *  node in the URL, so without this the page has nothing to fall back to and
   *  waits on a read that is never coming. */
  resolveError?: boolean;
  /** Re-runs the reads `resolveError` reports on. */
  onRetryResolve?: () => void;
  /** The place the browser slot is currently showing, so the rail marks it current. */
  place?: FilesPlaceId;
  /** Told about every rail click, before the shell routes a node-addressed place. */
  onSelectPlace?: (place: FilesPlaceId) => void;
}

export function FilesPage({
  trash = false,
  toolbar,
  breadcrumb,
  browser,
  pane,
  placeNodeIds,
  virtualPlaces,
  driveReady = false,
  resolveError = false,
  onRetryResolve,
  place,
  onSelectPlace,
}: FilesPageProps) {
  const { nodeId } = useParams<{ nodeId: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const home = useFilesHome();
  const narrow = useNarrow();
  const [sheetOpen, setSheetOpen] = useState(false);

  // A pane that arrives while the layout is narrow opens the sheet; losing the pane closes it.
  useEffect(() => {
    if (pane === undefined || pane === null) setSheetOpen(false);
  }, [pane]);

  const atIndex = !trash && nodeId === undefined;
  // A feed is a view over the whole drive, so it is a destination rather than a
  // stop on the way to the home folder: `/files?place=sharedWithMe` is where a
  // reader who cannot see the folder a file lives in is sent, and redirecting it
  // into their home would drop them somewhere they did not ask for.
  const feedOnly = atIndex && isFeedPlace(place);
  if (atIndex && !feedOnly && typeof home.nodeId === "string") {
    return (
      <Navigate to={`/files/${home.nodeId}${location.search}`} replace state={location.state} />
    );
  }

  const current = currentPlace({ trash, nodeId, place, placeNodeIds });
  const reachable = [
    ...(placeNodeIds ? (Object.keys(placeNodeIds) as FilesPlaceId[]) : []),
    ...(virtualPlaces ?? []),
  ];

  const rail = (
    <Sidebar
      current={current}
      reachable={reachable}
      driveReady={driveReady}
      onSelect={(chosen) => {
        onSelectPlace?.(chosen);
        const target = placeNodeIds?.[chosen];
        if (target) navigate(`/files/${target}`);
      }}
    />
  );
  const listing = (
    // A region, not a second `<main>`: the app shell already draws the page's one.
    <section className="alk-files__browser" aria-label={trash ? "Trash" : "Files"}>
      {atIndex && !feedOnly && resolveError ? (
        <DriveResolving error onRetry={() => onRetryResolve?.()} />
      ) : atIndex && !feedOnly && home.nodeId === undefined ? (
        <p className="alk-files__resolving">Opening your files…</p>
      ) : atIndex && !feedOnly && home.nodeId === null ? (
        <p className="alk-files__resolving">You do not have a home folder in this drive yet.</p>
      ) : (
        browser
      )}
    </section>
  );
  const hasPane = pane !== undefined && pane !== null;

  return (
    <div className="alk-files" data-layout={narrow ? "narrow" : "wide"}>
      <header className="alk-files__head">
        <div className="alk-files__trail">{breadcrumb}</div>
        <div className="alk-files__toolbar">{toolbar}</div>
      </header>
      <div className="alk-files__body">
        {narrow ? (
          <>
            {rail}
            {listing}
            {hasPane ? (
              <aside
                className="alk-files__pane"
                data-sheet="true"
                role="dialog"
                aria-label="Details"
                hidden={!sheetOpen}
              >
                {pane}
              </aside>
            ) : null}
          </>
        ) : (
          <FilesSplit rail={rail} listing={listing} pane={hasPane ? pane : null} />
        )}
      </div>
      {pane !== undefined && pane !== null && narrow ? (
        <button
          type="button"
          className="alk-files__sheet-toggle"
          onClick={() => setSheetOpen((open) => !open)}
        >
          {sheetOpen ? "Hide details" : "Details"}
        </button>
      ) : null}
    </div>
  );
}

export default FilesPage;

/* ------------------------------------------------------------------------- *
 * The assembly.
 *
 * `FilesScreen` is what the route renders: it owns the reads, the filter state,
 * the selection, the upload session and the undo stack, and hands every surface
 * to the shell above as a slot. The shell stays a frame with no knowledge of
 * where a row comes from, which is what lets each surface keep its own test.
 * ------------------------------------------------------------------------- */

/** The rail's node-addressed places, resolved from the drive's root listing.
 *  The server names the root containers; the client never spells a path.
 *
 *  `home` is deliberately NOT among them. The root's `home` child is the org-wide
 *  container that holds EVERY member's home folder — an org admin outranks it, so
 *  taking it for "my files" puts colleagues' private folders, labelled by their
 *  addresses, on her own landing screen. The caller's own home is `/home/<me>`,
 *  which only the server can name, and it arrives as the drive's `homeId`. */
export function placesFromRoots(rows: readonly Item[]): Partial<Record<FilesPlaceId, string>> {
  const byName: Partial<Record<FilesPlaceId, string>> = {};
  for (const row of rows) {
    const name = row.name.toLowerCase();
    if (name === "shared") byName.shared = row.id;
    else if (name === "teams") byName.teams = row.id;
  }
  return byName;
}

/** What each feed calls itself, and what it says when it holds nothing. */
export const FEED_TEXT: Record<FeedPlaceId, { label: string; empty: string }> = {
  recent: { label: "Recent", empty: "Nothing here yet. Files you open show up here." },
  sharedWithMe: {
    label: "Shared with me",
    empty: "Nobody has shared anything with you in this drive.",
  },
};

/** A feed arrives in the server's own order, newest first. */
const FEED_ORDER = { field: "mtime", direction: "desc" } as const;

function noSort(): void {
  /* a feed is not re-orderable: the route decides what "recent" means */
}

export interface FeedListProps {
  driveId: string | undefined;
  place: FeedPlaceId;
  /** Opening a feed row leaves the feed: a row is a real node, so it goes to
   *  the folder it actually lives in rather than staying in a listing that has
   *  no parent to act against. */
  onOpen?: (item: Item) => void;
  /** Clicking a row's Location opens the folder it lives in, which is the
   *  shortest way out of a feed and into the place the row actually is. */
  onOpenLocation?: (nodeId: string) => void;
  /** What is selected here, as ids — the same currency the folder listing
   *  reports in. Ids outlive a refetch; the row objects do not, and a page
   *  holding those went on showing the name a row had when it was clicked. */
  onSelectionChange?: (ids: readonly string[]) => void;
  /** The feed's rows as they stand, so the page can resolve its selected ids
   *  against THIS second's rows: a feed has no parent node, so the page cannot
   *  fetch the listing itself. */
  onRowsChange?: (rows: readonly Item[]) => void;
  /** Drawn in place of a row's name — the inline rename editor. A feed row
   *  offers Rename in its menu like any other, so it renders the editor like
   *  any other. */
  renderNameOverride?: (item: Item) => React.ReactNode | null;
  rowAdornment?: (item: Item) => React.ReactNode;
  platform?: Platform;
  initialRect?: { width: number; height: number };
}

/**
 * One feed, in the same treegrid the folder listing uses.
 *
 * A feed has no parent node, so it offers none of the actions that need one —
 * no create, no drop target, no breadcrumb. What it keeps is the part a person
 * came for: the same rows, the same columns, the same keyboard, and Enter on a
 * row opening the node where it really lives.
 */
export function FeedList({
  driveId,
  place,
  onOpen,
  onOpenLocation,
  onSelectionChange,
  onRowsChange,
  renderNameOverride,
  rowAdornment,
  platform,
  initialRect,
}: FeedListProps) {
  // Two reads, one enabled: each hook takes the drive alone, and the one that
  // is not this place is handed `undefined`, which leaves it idle.
  const recent = useRecent(place === "recent" ? driveId : undefined);
  const shared = useSharedWithMe(place === "sharedWithMe" ? driveId : undefined);
  const query = place === "recent" ? recent : shared;
  const rows = useMemo(() => query.data?.value ?? [], [query.data]);

  const [selection, setSelection] = useState(emptySelection);
  const onSelectionAction = useCallback(
    (action: SelectionAction) => {
      setSelection((current) =>
        selectionReducer(
          current,
          action,
          rows.map((row) => row.id),
        ),
      );
    },
    [rows],
  );

  // Reported after the commit and never from inside the updater, for the same
  // reason the folder browser reports its own there: a parent setState during
  // the render phase is the "cannot update a component while rendering a
  // different component" error, which a right-click hits on every open.
  const notify = useRef(onSelectionChange);
  useEffect(() => {
    notify.current = onSelectionChange;
  }, [onSelectionChange]);
  const selected = selection.selected;
  const notified = useRef(selected);
  useEffect(() => {
    if (notified.current === selected) return;
    notified.current = selected;
    notify.current?.([...selected]);
  }, [selected]);

  // And the rows themselves, every time the feed answers again: a refetch is
  // how a renamed row reaches the page, and rows resolved once at click time
  // would go on naming it whatever it was called then.
  const report = useRef(onRowsChange);
  useEffect(() => {
    report.current = onRowsChange;
  }, [onRowsChange]);
  useEffect(() => {
    report.current?.(rows);
  }, [rows]);

  const text = FEED_TEXT[place];
  if (query.isPending) {
    return (
      <p className="alk-files__resolving" aria-busy="true">
        Loading {text.label.toLowerCase()}…
      </p>
    );
  }
  if (query.isError) {
    return <p className="alk-files__resolving">{text.label} could not be loaded.</p>;
  }
  if (rows.length === 0) {
    return <p className="alk-files__resolving">{text.empty}</p>;
  }
  return (
    <Treegrid
      rows={rows}
      view="list"
      selection={selection}
      onSelectionAction={onSelectionAction}
      // The feed's order is the server's; there is no second listing to page,
      // so the header reports the order it is showing and does not re-ask.
      orderBy={FEED_ORDER}
      onSort={noSort}
      onOpen={onOpen}
      // A feed's rows come from all over the drive, so each one says which folder it
      // is in — without it two files of the same name are two rows that look alike.
      showLocation
      {...(onOpenLocation ? { onOpenLocation } : {})}
      renderNameOverride={renderNameOverride}
      rowAdornment={rowAdornment}
      platform={platform}
      label={text.label}
      {...(initialRect ? { initialRect } : {})}
    />
  );
}

/** A picked `FileList` read as the same narrow surface a drop arrives on.
 *
 *  A picker gives flat files with `webkitRelativePath`, never entries, so the
 *  folder skeleton is rebuilt from those paths as synthetic entries — which is
 *  what lets one `tree` call recreate it. The one thing a picker cannot report
 *  is an EMPTY directory: the browser omits it from the list entirely, so a
 *  drop stays the only way to upload one. */
export function pickedTransfer(files: readonly File[]): DropTransfer {
  const roots = new Map<string, DropEntry>();
  const items: DropItem[] = [];
  for (const file of files) {
    const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath ?? "";
    const segments = relative ? relative.split("/") : [file.name];
    if (segments.length === 1) {
      items.push({ kind: "file", getAsFile: () => file });
      continue;
    }
    let level = roots;
    let entry: DropEntry | undefined;
    for (const [at, name] of segments.entries()) {
      const leaf = at === segments.length - 1;
      let next = level.get(name);
      if (!next) {
        next = leaf ? fileEntry(name, file) : directoryEntry(name);
        level.set(name, next);
      }
      if (at === 0) entry = next;
      if (!leaf) level = childrenOf(next);
    }
    if (entry && !items.some((held) => held.webkitGetAsEntry?.() === entry)) {
      items.push({ kind: "file", webkitGetAsEntry: () => entry });
    }
  }
  return { items, getData: () => "" };
}

/** The children a synthetic directory entry hands its reader, kept beside it so
 *  the tree can be grown one path at a time. */
const childLists = new WeakMap<DropEntry, Map<string, DropEntry>>();

function childrenOf(entry: DropEntry): Map<string, DropEntry> {
  let held = childLists.get(entry);
  if (!held) {
    held = new Map<string, DropEntry>();
    childLists.set(entry, held);
  }
  return held;
}

function directoryEntry(name: string): DropEntry {
  const entry: DropEntry = {
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      let drained = false;
      return {
        readEntries: (onSuccess) => {
          // A real reader signals the end with an empty batch, and the walk
          // drains it in a loop — so this one has to end the same way.
          const batch = drained ? [] : Array.from(childrenOf(entry).values());
          drained = true;
          onSuccess(batch);
        },
      };
    },
  };
  return entry;
}

function fileEntry(name: string, file: File): DropEntry {
  return { isFile: true, isDirectory: false, name, file: (onSuccess) => onSuccess(file) };
}

/** What the browser slot shows before the drive is known: a wait, or the
 *  refusal with a retry. Never a listing, because there is none to show yet. */
function DriveResolving({ error, onRetry }: { error: boolean; onRetry: () => void }) {
  if (!error) {
    return (
      <p className="alk-files__resolving" role="status">
        Opening your files…
      </p>
    );
  }
  return (
    <div className="alk-files__resolving" role="alert">
      <p>Your files could not be opened.</p>
      <button type="button" onClick={onRetry}>
        Try again
      </button>
    </div>
  );
}

/** The node read failed for a reason that is not an answer — a fault, a dropped
 *  connection, a server that is unwell. Said in the server's own words with a way
 *  to ask again, because unlike "not here" this one may be untrue a second later. */
function OpenRefused({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const copy = filesErrorCopy(error, { action: "read" });
  return (
    <div className="alk-files__resolving" role="alert" data-code={copy.code}>
      <p>{copy.title}</p>
      {copy.detail ? <p>{copy.detail}</p> : null}
      <button type="button" onClick={onRetry}>
        Try again
      </button>
    </div>
  );
}

export interface FilesScreenProps {
  trash?: boolean;
  /** Test seam: the platform the shortcut table is read on. Unset resolves
   *  it from the browser. */
  platform?: Platform;
  /** Show a file's bytes without leaving the listing. Unwired, a file the server
   *  renders opens in a tab of its own, which is what this page could do before
   *  a preview surface existed. */
  onPreview?: (item: Item) => void;
}

export function FilesScreen({ trash = false, platform, onPreview }: FilesScreenProps) {
  // Unset means "read this browser": a fixed default binds the whole shortcut
  // table to the wrong accelerator on macOS, where Cmd+Z then does nothing.
  const platformHere = useMemo(() => platform ?? detectPlatform(), [platform]);
  const { nodeId } = useParams<{ nodeId: string }>();
  const navigate = useNavigate();
  const [filters, setFilters] = useFilterState();
  const [params, setParams] = useSearchParams();
  // The live query, for the callbacks that write it: reading `params` out of a
  // closure would rebuild every handler on each navigation and still hand an
  // older copy to anything that had already captured one.
  const paramsRef = useRef(params);
  paramsRef.current = params;

  const drive = useDrive();
  const driveId = drive.data?.id;
  const rootId = drive.data?.rootId;

  const roots = useChildren(driveId, rootId);
  const rootRows = useMemo(() => flattenChildren(roots.data), [roots.data]);
  // The server resolves whose home is whose; the client only routes to the id it is
  // handed. `null` once the drive has answered without one — this caller has no home
  // folder — so the shell says so instead of waiting forever.
  //
  // Routed to sight unseen, though, so a home that turns out not to be a folder
  // would be opened AS one — its name in the trail over a listing of nothing, which
  // is what a founder saw the day the server matched a receipt she had dropped into
  // the `/home` container. A place a person cannot browse is no place at all: once
  // the node the drive named is known to be something else, Home is withdrawn from
  // the rail and the page lands on the root listing instead (below).
  const [unbrowsableHomeId, setUnbrowsableHomeId] = useState<string | null>(null);
  const servedHomeId = drive.data?.homeId;
  const homeNodeId =
    typeof servedHomeId === "string" && servedHomeId !== unbrowsableHomeId
      ? servedHomeId
      : drive.isSuccess
        ? null
        : undefined;
  const placeNodeIds = useMemo(() => {
    const places = placesFromRoots(rootRows);
    return homeNodeId ? { ...places, home: homeNodeId } : places;
  }, [rootRows, homeNodeId]);
  // `/files` carries no node, so the only way out of it is the root listing —
  // and three answers end that listing with nowhere to go: the drive read
  // refused, the drive came back naming no root node (the listing is then never
  // even asked for, so no request fails and nothing on the page moves), or the
  // listing itself refused. All three are one refusal with a retry, so the
  // page never sits on "Opening your files…" for ever.
  const driveUnresolved = drive.isSuccess && (rootId === undefined || rootId === "");
  const resolveError = drive.isError || driveUnresolved || roots.isError;
  const retryResolve = useCallback(() => {
    void drive.refetch();
    void roots.refetch();
  }, [drive, roots]);

  const listFilters = useMemo(() => toListFilters(filters), [filters]);
  // The URL is the single source of the order, so the grid, the search and the
  // soft-threshold footer all page the SAME listing — a header click that only moved
  // the grid would leave the footer counting and paging a listing nobody can see.
  const onSort = useCallback(
    (field: OrderField, direction: OrderDirection) => {
      setFilters({ ...filters, orderBy: { field, direction } });
    },
    [filters, setFilters],
  );
  // The node the URL names — a folder, or a file somebody shared a link to.
  const node = useItem(driveId, nodeId);
  // A link to a FILE lands on the folder that holds it with the file open over
  // the listing, so the rows below are that folder's. When the URL names a
  // folder the two reads are the same query and cost one request, not two.
  const landedFile = node.data?.kind === "file" ? node.data : undefined;
  const fileParentId = landedFile?.parentId ?? undefined;
  const folder = useItem(driveId, landedFile ? fileParentId : nodeId);
  const landing = landingState({
    node: node.data,
    nodeError: node.error,
    parent: landedFile ? folder.data : undefined,
    parentError: landedFile ? folder.error : undefined,
  });
  /** The folder whose rows are on screen, or nothing when there are none to
   *  show: a file shared alone has no listing, and an id that answers 404 must
   *  not be asked for its children on top of the read that already said no. */
  const listFolderId =
    landing === "folder" ? nodeId : landing === "file-in-folder" ? fileParentId : undefined;
  // The notebook editor lives only in a chat's workspace pane, so a notebook is
  // offered here only where the server names the chat that would run it: in a
  // workspace's folder or a chat's own, and only to someone who may send there.
  // Anywhere else no kernel can exist, and a notebook made there could never run.
  const notebookChat = useNotebookEditor(driveId, listFolderId).data?.chat_id ?? null;
  // The folder above, read only to tell whether the page is on a folder-object's
  // working directory: a chat and a template each name that node on their facet,
  // and the page then dresses the listing as the thing itself — its title on the
  // trail, its notice above the rows. Keyed on the pair rather than on chats, so
  // walking into a template's files does not show the person the working folder's
  // own machine-minted name.
  const above = useItem(driveId, folder.data?.parentId ?? undefined);
  const container = above.data;
  const dressedAsChat =
    container !== undefined &&
    folder.data !== undefined &&
    isFolderObject(container) &&
    folder.data.parentId === container.id &&
    browseTargetOf(container) === folder.data.id
      ? container
      : undefined;
  // A file shared alone is its own trail: there is exactly one segment, because
  // the folder above it is not the reader's to name.
  const chain = useCrumbTrail(landing === "file-solo" ? node.data : folder.data, dressedAsChat);
  // Not a place files live: offer nothing that would put one here.
  const onSignpost = folder.data
    ? isSignpost(folder.data, rootId)
    : nodeId !== undefined && nodeId === rootId;
  // The one read that can tell: the node the page is on IS the drive's home, and
  // it is not a folder. No extra request in the healthy case — the landing page
  // reads the node it opened anyway.
  const opened = node.data;
  useEffect(() => {
    if (opened !== undefined && opened.id === servedHomeId && opened.kind !== "folder") {
      setUnbrowsableHomeId(opened.id);
    }
  }, [opened, servedHomeId]);

  const threshold = useSoftThreshold(driveId, listFolderId, {
    filters: listFilters,
    orderBy: filters.orderBy,
  });
  const search = useSearchQuery({
    driveId,
    folderId: listFolderId,
    scope: filters.scope,
    text: filters.text,
    filters: listFilters,
    orderBy: filters.orderBy,
  });
  // A leased folder is being written from somewhere else — a machine holding it
  // for a chat, a mount on somebody's laptop — so this listing is a view of
  // another disk. The judgement is `liveState`'s, the frames that move it are
  // this hook's, and the rows it counts are the listing's own: the same query
  // key the browser below reads, so the count costs no second request.
  const listedRows = useChildren(driveId, listFolderId, {
    filters: listFilters,
    orderBy: filters.orderBy,
  });
  const onBox = useMemo(() => countOnBox(flattenChildren(listedRows.data)), [listedRows.data]);
  // The loaded rows the browser below keeps out of sight, by the same rule and
  // the same viewer choice, so the footer counts what the listing draws.
  const [showHidden] = useShowHiddenFiles();
  const hiddenCount = useMemo(
    () =>
      threshold.rows.length -
      visibleRows(threshold.rows, chatRecordsRule(folder.data), showHidden).length,
    [threshold.rows, folder.data, showHidden],
  );
  const openFolderIds = useMemo(
    () => (listFolderId === undefined ? [] : [listFolderId]),
    [listFolderId],
  );
  const liveness = useFolderLiveness(driveId, listFolderId, openFolderIds, onBox);
  // A machine holding this folder is its writer. When its lease admits writes
  // from the web they are offered here as in the chat's Files tab -- the drive
  // hands them to the machine and keeps both sides when the two cross -- and
  // only a lease that does not admit them makes the rows the saved copy, every
  // write refused rather than raced. The verdict is read off the folder's own
  // facet at this tick, and off each row's for the rows (a row can be a leased
  // folder itself, and a feed lists rows from many folders). The lease's frames
  // are subscribed, so this comes back on its own the moment the lease ends.
  const now = useNow();
  const leasedHere = refusesWebWrites(folder.data, now);
  const isHeld = useCallback((item: Item) => refusesWebWrites(item, now), [now]);
  // The chat holding the folder, named above the rows with a link for the
  // people the chat's own policy admits, whether or not the web may write.
  const leasingChat = isHeldByMachine(folder.data, now) ? chatLeaseOf(folder.data) : null;
  // Unfinished uploads are remembered under the reader and the org they are in.
  const uploader = userScope(useCurrentUser().data);
  const uploads = useUploads({ driveId: driveId ?? "", account: uploader });
  const undoStack = useUndoStack();
  // The rows a drag that started on a row is carrying, for the life of the drag.
  // A drag-over can read only the transfer's TYPES, never its payload, so the
  // rows are remembered here and the payload is read back only on the drop.
  const dragging = useRef<readonly Item[]>([]);

  const [selectedIds, setSelectedIds] = useState<readonly string[]>([]);
  /** What a surface that keeps its OWN listing reported — a feed. The folder
   *  browser's selection is `selectedIds` resolved against the page's own rows;
   *  a feed has no parent node and reads its rows itself, so it reports both:
   *  the ids selected and the rows it currently holds. The selection is resolved
   *  the same way on both surfaces, which is what keeps the details pane on the
   *  row as it stands rather than on the copy that was there at click time. */
  const [feedIds, setFeedIds] = useState<readonly string[]>([]);
  const [feedRows, setFeedRows] = useState<readonly Item[]>([]);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  /** True between walking into a folder and the new listing taking the focus
   *  the walk left adrift. The listing clears it, whether it took the focus or
   *  found that something else had claimed it. */
  const [resumeFocus, setResumeFocus] = useState(false);
  const onFocusResumed = useCallback(() => setResumeFocus(false), []);
  // Which feed is showing is in the URL, so it is linkable and survives a
  // reload — "Shared with me" is where a file shared without its folder sends
  // the reader, and a place held in component state could not be linked to.
  const place = virtualPlaceFrom(params.get("place"));
  const showPlace = useCallback(
    (next: VirtualPlaceId) => {
      const query = new URLSearchParams(params);
      query.set("place", next);
      setParams(query);
    },
    [params, setParams],
  );
  /** What the create field is naming, while it is open. */
  const [naming, setNaming] = useState<"folder" | "notebook" | null>(null);
  /** A notebook's upload is out: the name is spent until it lands. */
  const [creatingNotebook, setCreatingNotebook] = useState(false);
  /** Why the last New folder was refused. A create that answers 507 (no room)
   *  or 409 (that name is taken) changes nothing, so with no `onError` the form
   *  simply closed and the folder was never there. */
  const [createRefusal, setCreateRefusal] = useState<FilesErrorCopy | null>(null);
  /** What is in the New folder field, so the same non-blocking note the rename
   *  editor shows can be written under it. The name is chosen here more often
   *  than it is changed later, and a field that says nothing about `CON` while
   *  the rename beside it does makes the two disagree about one rule. */
  const [createDraft, setCreateDraft] = useState("");
  const createFolder = useCreateFolder();
  const filePicker = useRef<HTMLInputElement | null>(null);
  const folderPicker = useRef<HTMLInputElement | null>(null);
  const paneRef = useRef<HTMLDivElement | null>(null);
  const browserRef = useRef<HTMLDivElement | null>(null);
  const toolbarRef = useRef<HTMLDivElement | null>(null);

  const rowsById = useMemo(
    () => new Map(threshold.rows.map((row) => [row.id, row])),
    [threshold.rows],
  );
  // Which of the surfaces `renderBrowserBody` can draw is on screen. The folder
  // browser is the only one whose rows the page itself holds; a feed and a search
  // hold their own and report them, and My leases and the trash have no selection
  // to report at all.
  const feedDrawn = !trash && isFeedPlace(place);
  // A search draws wherever it is active and neither a feed nor My leases is,
  // which is the order `renderBrowserBody` resolves them in.
  const searchDrawn = !trash && !feedDrawn && place !== "leases" && search.isActive;
  const reportsOwnRows = feedDrawn || searchDrawn;
  const browserDrawn = !trash && place === undefined && !search.isActive;
  // Which surface is drawing the rows the selection belongs to. The page keeps
  // ONE selection and swaps what `renderBrowserBody` draws underneath it, so a
  // feed, a search, My leases and the trash all have to be named here — and so
  // does walking into another folder.
  const selectionSurface = trash
    ? "trash"
    : place !== undefined
      ? `place:${place}`
      : search.isActive
        ? "search"
        : `folder:${listFolderId ?? ""}`;
  useEffect(() => {
    // Leaving a surface leaves its selection behind. Without this the menu, the
    // keyboard and the details pane went on acting on the folder listing hidden
    // under a feed — and a row moved to the trash from a listing nobody is
    // looking at is not a mistake a person can see being made.
    setSelectedIds([]);
    setFeedIds([]);
    setFeedRows([]);
    // A rename the reader started and left behind belongs to the surface they
    // started it on. Carried across, the pending id waits for the next listing
    // that happens to hold that node and opens an editor on a row nobody asked
    // to rename.
    setRenamingId(null);
  }, [selectionSurface]);

  /** The rows every action is aimed at: only ever rows the reader can see.
   *  A feed and a search each report their own, because the rows they draw come
   *  from all over the drive and the page holds none of them. */
  const feedRowsById = useMemo(() => new Map(feedRows.map((row) => [row.id, row])), [feedRows]);
  const selection = useMemo(
    () =>
      (reportsOwnRows ? feedIds : browserDrawn ? selectedIds : [])
        .map((id) => (reportsOwnRows ? feedRowsById : rowsById).get(id))
        .filter((row): row is Item => row !== undefined),
    [reportsOwnRows, browserDrawn, feedIds, feedRowsById, selectedIds, rowsById],
  );
  const active = selection[0];

  /** How much of the folder the listing is holding, so a select-all can say
   *  whether it reached the whole folder. Select-all reaches the rows that are
   *  paged in and nothing else — in a folder of thousands that is a fraction,
   *  and a bar that reported it as a flat count let a person send Move to trash
   *  on a third of a folder believing it was all of it. */
  const selectionScope = useMemo(
    () => ({ loaded: threshold.loaded, total: threshold.total, hasMore: threshold.hasMore }),
    [threshold.loaded, threshold.total, threshold.hasMore],
  );
  /** Set while the rest of the folder is being paged in for a select-all, so
   *  the selection widens to the whole folder once the last marker lands. */
  const [selectingRest, setSelectingRest] = useState(false);
  const selectRest = useCallback(() => {
    setSelectingRest(true);
    // A bounded budget, not an unbounded one: the bar offers this only for a
    // folder that fits the listing's own ceiling, and `loadMore` raises the
    // ceiling by exactly that much rather than lifting it entirely — so an
    // aggregate that under-reports cannot turn the click into unbounded paging.
    threshold.loadMore();
  }, [threshold]);
  useEffect(() => {
    if (!selectingRest) return;
    // Still paging: the rows that have landed are already selected below, and
    // the selection grows with them until the listing ends.
    setSelectedIds(threshold.rows.map((row) => row.id));
    if (!threshold.hasMore) setSelectingRest(false);
  }, [selectingRest, threshold.rows, threshold.hasMore]);
  // A walk to another folder abandons a select-all that was still paging: the
  // rows it would have selected are not the ones on screen any more.
  useEffect(() => setSelectingRest(false), [selectionSurface]);

  // The preview and the listing look at ONE row: stepping inside the preview
  // moves the selection under it, so closing drops the reader on the row they
  // ended on rather than the one they started from. Closing hands the id back
  // because the listing may have re-rendered since — the focus trap restores an
  // opener that still exists, and this finds the row again when it does not.
  const preview = useLinkedSelection({
    rows: threshold.rows,
    onSelect: (id) => setSelectedIds([id]),
    onReturnFocus: (id) => {
      const row = browserRef.current?.querySelector<HTMLElement>(
        `[data-row-id="${CSS.escape(id)}"]`,
      );
      row?.focus();
    },
  });
  // Opening a file is going somewhere, so it has an address: the row and the
  // kind of preview it opens. That is what makes a preview linkable, survive a
  // reload, and — the reason it matters — close on the Back button, which is
  // what a reader who opened a file over a listing reaches for first.
  const showPreview = useCallback(
    (item: Item) => {
      preview.openOn(item);
      const query = new URLSearchParams(paramsRef.current);
      query.set(PREVIEW_PARAM, item.id);
      query.set(PREVIEW_KIND_PARAM, previewKindOf(item));
      // Pushed, not replaced: Back has to have something to come back from.
      setParams(query);
    },
    [preview, setParams],
  );

  // A link to a file opens the file. The reader clicked the name of a report,
  // not the name of a folder, so the bytes are what they get — over the listing
  // where the file lives, when that folder is theirs to see, and alone when it
  // is not. The id it was opened for is remembered so the URL closes exactly the
  // preview it opened and never one the reader opened themselves afterwards.
  const openedByLink = useRef<string | null>(null);
  const previewApi = useRef(preview);
  previewApi.current = preview;
  const landedId = node.data?.id;
  const landedOnFile = landing === "file-in-folder" || landing === "file-solo";
  useEffect(() => {
    if (!landedOnFile || landedId === undefined) {
      // Walked off the file — closing it replaced the URL with the folder — so
      // the preview the link opened goes with it.
      if (openedByLink.current !== null) {
        openedByLink.current = null;
        previewApi.current.close();
      }
      return;
    }
    if (openedByLink.current === landedId) return;
    openedByLink.current = landedId;
    const file = node.data;
    if (file) previewApi.current.openOn(file);
  }, [landedOnFile, landedId, node.data]);

  // The two directions the address and the sheet keep each other in step.
  //
  // Forward: stepping to the next row inside the sheet is a new preview, so the
  // address follows it — replaced rather than pushed, because Back should leave
  // the preview, not walk the rows backwards one Back at a time. Closing drops
  // the parameters.
  //
  // Backward: the parameters gone with the sheet still open is the Back button,
  // and it closes the sheet. A preview a LINK opened has no parameters of its
  // own — the file's own URL is its address — so it is left to `closePreview`.
  const previewItem = preview.item;
  const previewParam = params.get(PREVIEW_PARAM);
  useEffect(() => {
    if (previewParam === null) return;
    const query = new URLSearchParams(paramsRef.current);
    if (previewItem !== undefined) {
      if (previewParam === previewItem.id) return;
      query.set(PREVIEW_PARAM, previewItem.id);
      query.set(PREVIEW_KIND_PARAM, previewKindOf(previewItem));
    } else {
      query.delete(PREVIEW_PARAM);
      query.delete(PREVIEW_KIND_PARAM);
    }
    setParams(query, { replace: true });
  }, [previewItem, previewParam, setParams]);
  useEffect(() => {
    if (previewParam !== null) return;
    if (openedByLink.current !== null) return;
    if (previewApi.current.open) previewApi.current.close();
  }, [previewParam]);

  // A previewed document names the files stored beside it — `charts/q3.png`,
  // `[the numbers](salaries.csv)` — and only something that can walk THIS
  // folder turns those names into bytes and into rows. The walk is contained to
  // the folder the listing is standing in, so a document can never reach a name
  // the reader could not have listed themselves; a folder the server refuses
  // (a file shared without the folder around it) gets no resolver at all, which
  // is what keeps its references from becoming a probe for names.
  const queryClient = useQueryClient();
  const grant = useMintContentGrant();
  const latestGrant = useRef(grant.mutateAsync);
  latestGrant.current = grant.mutateAsync;
  const mintForPreview = useCallback<MintContentGrant>((vars) => latestGrant.current(vars), []);
  const listedFolder = folder.data;
  /** The row a reference inside the sheet asked for. It rides the listing's own
   *  reveal channel, which is what actually moves the grid's selection. */
  const [revealedId, setRevealedId] = useState<string | null>(null);
  const previewResolver = useMemo(
    () =>
      listedFolder && listFolderId === listedFolder.id
        ? // Clicking a reference means "show me that one instead": the row it
          // names is selected in the listing and the sheet moves onto it, the
          // same answer the Files tab beside a chat gives.
          folderResolver(listedFolder, mintForPreview, queryClient, (item) => {
            setRevealedId(item.id);
            previewApi.current.openOn(item);
          })
        : undefined,
    [listedFolder, listFolderId, mintForPreview, queryClient],
  );
  // The bytes a reference bought live as long as the sheet showing them. Held
  // past the close they would be a page keeping a file in memory for the life
  // of the tab.
  const previewOpen = preview.open;
  useEffect(() => {
    if (!previewOpen || !previewResolver) return;
    return () => previewResolver.revokeAll();
  }, [previewOpen, previewResolver]);

  /** Closing the preview a link opened leaves the listing it was opened over,
   *  with the URL saying so — replaced rather than pushed, so Back goes where
   *  the reader came from instead of reopening the file.
   *
   *  A file shared ALONE has no such listing: its folder is the read that was
   *  refused, and the id the file carries names it all the same. Walking there
   *  would land the reader on "this isn't here" — and `replace` would spend the
   *  one URL that reaches the file doing it. So closing leaves them on the card,
   *  the same asymmetry "Open in Files" is withheld for. */
  const closePreview = useCallback(() => {
    const linked = openedByLink.current === landedId && landedId !== undefined;
    preview.close();
    if (linked && landing !== "file-solo" && fileParentId !== undefined) {
      openedByLink.current = null;
      navigate(`/files/${fileParentId}`, { replace: true });
    }
  }, [preview, landedId, landing, fileParentId, navigate]);

  // Drag and drop is one surface's worth of rules, not the page's: every drop
  // the listing accepts, the ring a row shows, and the listing's own drop zone.
  const { dropOn, dragDropFor, listingProps, listingDropActive, hereName } = useFilesDragDrop({
    driveId,
    folder: folder.data,
    folderId: listFolderId,
    // Walked into a chat, the folder is named after the conversation.
    displayAs: dressedAsChat,
    canWriteHere: (folder.data?.capabilities?.can_write ?? false) && !onSignpost,
    // A folder a machine holds is read-only on this page: a drop into one, or
    // out of one, is refused before any request. The chat's own Files tab is
    // where such a drop is meant to land, and it leaves this unset.
    refuseHeld: true,
    rows: threshold.rows,
    selection,
    uploads,
    dragging,
  });

  // Going to a node leaves whatever view was over the listing behind: the path
  // carries no `?place=`, so the feed ends with the navigation rather than
  // needing a second piece of state cleared in step with it.
  const openNode = useCallback(
    (id: string) => {
      // The folder being opened replaces every row, so the row the keyboard was
      // on goes with them and the browser drops the focus out to the document.
      // The listing below is told to take it back when its own rows arrive —
      // without it the next keystroke reached nothing, and Cmd+Up, the way back
      // out, did nothing until a row had been clicked.
      setResumeFocus(true);
      navigate(`/files/${id}`);
    },
    [navigate],
  );
  /** The page's inline refusal line, captured from the wired api: a refusal
   *  decided up here has to reach the same place a refused write does, and the
   *  api is only in scope inside the render prop below. */
  const refuseHere = useRef<FilesActionsApi["refuse"] | null>(null);
  /** The folder above the one on screen, or a refusal when there is none.
   *
   *  The trail is the path this session walked, so a deep link has a single
   *  segment and the folder's own parent is the only answer there; a file shared
   *  alone has no answer at all, and says so rather than doing nothing. */
  const openParent = useCallback(() => {
    const walked = chain[chain.length - 2]?.id;
    const up =
      walked ?? (landing === "file-solo" ? undefined : (folder.data?.parentId ?? undefined));
    if (up !== undefined && up !== null && up !== "") {
      openNode(up);
      return;
    }
    refuseHere.current?.(NO_PARENT_HERE);
  }, [chain, landing, folder.data, openNode]);
  // The control's answer to the same question, without the refusal: on the
  // drive's own root and on a file shared alone it is offered disabled.
  const canGoUp =
    landing !== "file-solo" &&
    (chain.length > 1 || (folder.data?.parentId !== null && folder.data?.parentId !== undefined));
  const parentAbove =
    chain[chain.length - 2]?.name ?? (above.data ? displayNameOf(above.data) : undefined);
  const upTitle = canGoUp && parentAbove ? `Up to ${displayPlaceName(parentAbove)}` : "Up";
  // Opening a plain file RENDERS it when the server will render it — a PDF an
  // agent produced is opened to be read, and a double-click that did nothing was
  // the only answer this page had. A new tab rather than this one: the listing,
  // the selection and the place stay where the person left them, and the bytes
  // are served from the content origin under their own CSP. The gesture is the
  // person's double-click, so no popup blocker stands in front of it.
  const openItem = useCallback(
    (item: Item) => {
      // What "open" means for this row is decided once, for every gesture: a
      // chat, a workspace or a template opens on its page, a folder is listed.
      const target = openTargetOf(item);
      if (target.kind === "page") {
        navigate(target.to);
        return;
      }
      if (target.kind === "folder") {
        openNode(target.nodeId);
        return;
      }
      // A deliverable — the self-contained PDF or page an agent writes into a
      // chat's outputs — is opened to be READ: the preview draws it in place,
      // over the listing it was opened from, so the reader keeps their place and
      // can step to the next one. Unwired (a host that mounts no preview) the
      // bytes still render in a tab of their own, on the content origin, under
      // that origin's policy.
      const show = onPreview ?? showPreview;
      // A notebook opens in its editor, in the chat whose workspace pane runs
      // it (the chat a run would wake). One no kernel can reach is previewed.
      if (isNotebookName(item.name) && driveId !== undefined) {
        void queryClient
          .fetchQuery(notebookEditorQuery(driveId, item.id))
          .then((editor) => {
            if (editor.chat_id) navigate(chatFileHref(editor.chat_id, item.id));
            else show(item);
          })
          .catch(() => show(item));
        return;
      }
      if (isPreviewable(item)) {
        show(item);
        return;
      }
      const render = viewableUrl(item);
      if (render !== null) {
        window.open(render, "_blank", "noopener,noreferrer");
        return;
      }
      // Nothing draws it — a zip, a spreadsheet, a binary. The preview opens
      // anyway, on the card that says so and offers Download: a double-click
      // that silently saved bytes to ~/Downloads never told the reader what the
      // file was or why no picture came, and a double-click that did nothing at
      // all told them less. The card costs no request — a plan that needs no
      // bytes is ready without buying any.
      show(item);
    },
    [openNode, navigate, onPreview, showPreview, driveId, queryClient],
  );
  /** A folder-object's own page, at the address the server named on its facet.
   *  A row that names none has only its files, so it opens the way it always
   *  does. */
  const openObjectPage = useCallback(
    (item: Item) => {
      const door = pageDoorOf(item);
      if (door === null) {
        openItem(item);
        return;
      }
      navigate(door.to);
    },
    [openItem, navigate],
  );
  /** A brand-new chat off this template. The template's node is handed to the
   *  chat surface, which is what creates it: Files names the source and gets out
   *  of the way, so the reader lands in a chat with its rail and composer rather
   *  than in a listing watching a request. */
  const newChatFromTemplate = useCallback(
    (item: Item) => {
      navigate(`/chat?source=${encodeURIComponent(item.id)}`);
    },
    [navigate],
  );
  /** A folder-object's own files, listed like any folder's — at the working
   *  directory the server named, which is the effective root of the chat or the
   *  template. Nothing about the node changes: the grant on the object already
   *  covers everything beneath it. */
  const viewChatFiles = useCallback(
    (item: Item) => {
      openNode(browseTargetOf(item));
    },
    [openNode],
  );
  // Opening a search result reveals it: a folder opens itself, a file opens the folder
  // that holds it, which is where every action on it lives. A node that IS something
  // else opens that thing from here too — a chat found by name is opened, not revealed.
  const revealResult = useCallback(
    (item: Item) => {
      const target = openTargetOf(item);
      if (target.kind === "page") navigate(target.to);
      else if (target.kind === "folder") openNode(target.nodeId);
      else if (item.parentId) openNode(item.parentId);
    },
    [openNode, navigate],
  );

  const emptyAndFiltered =
    !trash && hasActiveFilters(filters) && !threshold.isFetching && threshold.rows.length === 0;

  // The New-folder form takes the create buttons' place in the browser bar while it is
  // open: a form above the trail pushed the trail, the header and every row down, and
  // the browser offered contact names for a field called "name". The refusal, if any,
  // hangs below the form (positioned in CSS) so even that moves nothing.
  // Not a refusal and never beside one: the drive takes these names, and a note
  // arguing with the reason the field is invalid would be two answers to one field.
  const createNote = createRefusal !== null ? null : nameNote(createDraft);
  // A create in flight holds the form: the name is spent, and a second Enter
  // would ask for the same folder again and be told it already exists.
  const creating = createFolder.isPending || creatingNotebook;
  const newFolderForm =
    naming !== null && listFolderId !== undefined && driveId !== undefined ? (
      <form
        className="alk-files__new-folder"
        aria-label={naming === "notebook" ? "New notebook" : "New folder"}
        // Escape cancels, as it does in the inline rename editor. The form opens
        // focused, so without this the only way out of it was the mouse.
        onKeyDown={(event) => {
          if (event.key !== "Escape") return;
          event.preventDefault();
          // The listing's own Escape clears the selection; this one is the form's.
          event.stopPropagation();
          setCreateRefusal(null);
          setCreateDraft("");
          setNaming(null);
        }}
        aria-busy={creating || undefined}
        onSubmit={(event) => {
          event.preventDefault();
          if (creating) return;
          const field = new FormData(event.currentTarget).get("folder");
          // The raw text: a surrounding space is one of the naming rules, and
          // trimming it away would create a folder nobody asked for.
          const name = typeof field === "string" ? field : "";
          // Nothing was typed at all — the same abandon Escape performs, not a
          // refusal to read.
          if (name === "") {
            setCreateRefusal(null);
            setCreateDraft("");
            setNaming(null);
            return;
          }
          // The shared rule table, the one the Files namespace answers with:
          // a name this refuses is refused here rather than spent on a request.
          const refused = validateName(name);
          if (refused !== null) {
            setCreateRefusal({
              code: `name_${refused.rule}`,
              title: refused.message,
              retryable: true,
            });
            return;
          }
          setCreateRefusal(null);
          if (naming === "notebook") {
            setCreatingNotebook(true);
            const parentId = listFolderId;
            // Loaded on first use, as the chat's Files pane loads it.
            void import("../chat/workspace/notebook/newNotebook")
              .then(({ createNotebook }) => createNotebook(driveId, parentId, name))
              .then((created) => {
                setCreateDraft("");
                setNaming(null);
                void queryClient.invalidateQueries({ queryKey: keys.files.childrenOf(driveId, parentId) });
                // Made to be written and run: it opens in its editor.
                if (notebookChat !== null) navigate(chatFileHref(notebookChat, created.nodeId));
              })
              // The field stays open holding what was typed, beside the reason.
              .catch((error: unknown) => setCreateRefusal(filesErrorCopy(error, { action: "create" })))
              .finally(() => setCreatingNotebook(false));
            return;
          }
          createFolder.mutate(
            { driveId, parentId: listFolderId, name },
            {
              onSuccess: () => {
                setCreateRefusal(null);
                setCreateDraft("");
                setNaming(null);
              },
              // The field stays open holding what was typed, next
              // to the reason — the folder does not exist, and
              // closing the form would say the opposite.
              onError: (error) => setCreateRefusal(filesErrorCopy(error, { action: "create" })),
            },
          );
        }}
      >
        <input
          name="folder"
          aria-label={naming === "notebook" ? "Notebook name" : "Folder name"}
          aria-describedby={createNote !== null ? "alk-files-new-folder-note" : undefined}
          value={createDraft}
          onChange={(event) => setCreateDraft(event.target.value)}
          readOnly={creating}
          autoFocus
          autoComplete="off"
        />
        <button type="submit" disabled={creating}>
          {creating ? "Creating…" : "Create"}
        </button>
        <button
          type="button"
          onClick={() => {
            setCreateRefusal(null);
            setCreateDraft("");
            setNaming(null);
          }}
        >
          Cancel
        </button>
        {createRefusal ? (
          <span className="alk-files__error" role="alert" data-code={createRefusal.code}>
            <strong>{createRefusal.title}</strong>
            {createRefusal.detail ? <span>{createRefusal.detail}</span> : null}
          </span>
        ) : null}
        {createNote !== null && (
          <span
            id="alk-files-new-folder-note"
            className="alk-files-rename__note"
            data-code={`name_${createNote.rule}`}
          >
            {createNote.message}
          </span>
        )}
      </form>
    ) : null;

  /** The folder a search can be narrowed to, named as the trail names it — a
   *  chat's working directory carries a machine-minted name the reader never
   *  sees, so the button says the chat. */
  const searchFolderName =
    place === undefined && listFolderId !== undefined && folder.data !== undefined
      ? displayNameOf(dressedAsChat ?? folder.data)
      : undefined;

  const toolbar = (
    <div className="alk-files__toolbar-slot" ref={toolbarRef}>
      <SearchBar
        state={filters}
        onChange={setFilters}
        // The narrow scope is offered only where there is a folder to narrow to:
        // a feed and the drive's home list rows from everywhere, so "this folder"
        // would name nothing.
        folderName={searchFolderName}
        platform={platformHere}
      />
    </div>
  );

  const footer = (
    <ThresholdFooter
      loaded={threshold.loaded}
      total={threshold.total}
      exact={threshold.exact}
      counting={threshold.counting}
      hasMore={threshold.hasMore}
      loadingAll={threshold.loadingAll}
      loadMore={threshold.loadMore}
      loadAll={threshold.loadAll}
      cancelLoadAll={threshold.cancelLoadAll}
      hidden={hiddenCount}
    />
  );

  const renderNameOverride = useCallback(
    (item: Item) => {
      if (item.id === renamingId && driveId !== undefined) {
        return (
          <RenameInline
            driveId={driveId}
            item={item}
            onDone={() => setRenamingId(null)}
            // The rename owns its mutation, so these are the only paths its
            // answer has to the stack: a rename becomes a step Cmd+Z can invert,
            // exactly like a copy or a trash.
            onOperation={(operation) =>
              undoStack.push({
                operationId: operation.id,
                driveId,
                kind: "rename",
                label: `Renamed ${item.nameDisplay || item.name}`,
                undoableUntil: operation.undoableUntil ?? undefined,
                etag: item.etag,
              })
            }
            // Which is what every rename actually is: the route queues only an
            // oversized move, so a rename is always answered with the node and
            // the operation branch above never fired. The inverse is the old
            // name, put back, fenced on the version the rename produced.
            onRenamed={(renamed, previousName) =>
              undoStack.push({
                driveId,
                kind: "rename",
                label: `Renamed ${previousName}`,
                rename: {
                  itemId: renamed.id,
                  fromName: previousName,
                  toName: renamed.name,
                  etag: renamed.etag,
                },
              })
            }
          />
        );
      }
      return null;
    },
    [renamingId, driveId, undoStack],
  );

  // What each row is doing on the machine right now — "writing…", "sending to
  // workspace…", "on the machine (2 MB)". It rides BESIDE the name rather than
  // replacing the name cell, because replacing it is how the grid is told a row
  // is being renamed, and a renaming row is not draggable. A row the drive
  // made to hold the version that lost a conflict needs no chip: its name
  // already says so.
  const rowAdornment = useCallback(
    (item: Item) => (
      <>
        <LiveRowChip live={item.live} lease={item.lease} />
      </>
    ),
    [],
  );

  // Built from the wired api rather than beside it: the pane's "…" button and
  // the Shift+F10 inside it open the page's ONE row menu, and a pane
  // constructed outside the render prop has no way to reach it.
  const renderPane = (acts: FilesActionsApi) => (
    <div className="alk-files__pane-slot" ref={paneRef} tabIndex={-1}>
      <RightPane
        driveId={driveId}
        item={active}
        selectedCount={selection.length}
        onOpenChat={openObjectPage}
        onViewFiles={viewChatFiles}
        triggerProps={acts.triggerProps}
        openMenuAt={acts.openMenuAt}
      />
    </div>
  );

  // The runner is the page's, so the bar's buttons take it as an argument:
  // a button and its context-menu twin then dispatch the SAME action id.
  // "Restore to…" needs a folder picker, which is the browser's Move-to dialog. The
  // trash page asks for one as a promise; the dialog settles it with the folder picked
  // or nothing on Cancel.
  const [pickSettle, setPickSettle] = useState<((folderId: string | undefined) => void) | null>(
    null,
  );
  const pickFolder = useCallback(
    () => new Promise<string | undefined>((resolve) => setPickSettle(() => resolve)),
    [],
  );
  const settlePick = (folderId: string | undefined) => {
    pickSettle?.(folderId);
    setPickSettle(null);
  };

  // Walked INTO a chat or a template: the rows below are not an ordinary folder's.
  // Normally that is the object's working directory dressed as the object; a chat
  // folder from before it had one lists itself, minus the box's runtime state.
  //
  // Only a template says so in a line of its own — its files are what a new chat
  // begins with, which nothing else on the page carries. A chat's listing says
  // nothing here: the live line below already names the chat holding the folder,
  // and a second sentence over the rows was the same fact said twice.
  const listingObject = dressedAsChat ?? (isFolderObject(folder.data) ? folder.data : undefined);
  const objectNotice =
    listingObject !== undefined && isTemplateFolder(listingObject) ? TEMPLATE_FILES_NOTICE : null;
  // Walked into a workspace's files: Open listed them, so the bar carries the
  // way to the workspace itself. Read off the facet of the folder the server
  // returned, never off a name or a path.
  const listingPage = secondPageDoorOf(listingObject);

  const renderBrowserBody = (acts: FilesActionsApi) =>
    trash ? (
      <>
        <TrashPage driveId={driveId} pickFolder={pickFolder} />
        <MoveToDialog
          open={pickSettle !== null}
          driveId={driveId}
          startFolderId={homeNodeId ?? undefined}
          startLabel="Home"
          count={1}
          title="Restore to…"
          confirmLabel="Restore here"
          platform={platformHere}
          onCancel={() => settlePick(undefined)}
          onConfirm={(parentId) => settlePick(parentId)}
        />
      </>
    ) : driveId === undefined ? (
      // Every read below keys on the drive id. Until it is known there is no
      // listing to count, no crumb to draw and no place to route — so the page
      // says it is still opening rather than painting "0 shown" over a folder
      // that holds rows; a refused drive read gets a way out that is not the URL.
      <DriveResolving error={drive.isError} onRetry={() => void drive.refetch()} />
    ) : place === "leases" ? (
      <MyLeases driveId={driveId} onOpen={openNode} streamDown={liveness.streamDown} />
    ) : isFeedPlace(place) ? (
      <FeedList
        driveId={driveId}
        place={place}
        onOpen={openItem}
        onOpenLocation={openNode}
        platform={platformHere}
        onSelectionChange={setFeedIds}
        onRowsChange={setFeedRows}
        renderNameOverride={renderNameOverride}
        rowAdornment={rowAdornment}
      />
    ) : search.isActive ? (
      <SearchResults
        result={search}
        onOpen={revealResult}
        onSelectionChange={setFeedIds}
        onRowsChange={setFeedRows}
        platform={platformHere}
      />
    ) : landing === "not-here" ? (
      <NotHere />
    ) : landing === "file-solo" && node.data !== undefined ? (
      // No listing, no create controls, and above all no path: the reader holds
      // a grant on these bytes and on nothing around them. The trail is the one
      // segment there is — the file itself — and it addresses nothing above it.
      <>
        <Breadcrumbs segments={chain} />
        <SoloItem
          item={node.data}
          onDownload={(file) => downloadItem(contentUrl(driveId, file.id), file)}
        />
      </>
    ) : landing === "error" ? (
      <OpenRefused error={node.error} onRetry={() => void node.refetch()} />
    ) : landing === "pending" ? (
      <p className="alk-files__resolving" role="status">
        Opening…
      </p>
    ) : emptyAndFiltered ? (
      <FilteredEmptyState state={filters} onChange={setFilters} />
    ) : (
      <FilesBrowser
        actions={
          onSignpost
            ? null
            : (newFolderForm ?? (
                <div className="alk-files-browser__creates" role="group" aria-label="Create">
                  {listingPage !== null && listingObject !== undefined ? (
                    <button
                      type="button"
                      className="alk-files-browser__create alk-files-browser__create--page"
                      onClick={() => openObjectPage(listingObject)}
                    >
                      {listingPage.label}
                    </button>
                  ) : null}
                  {CREATE_ACTIONS.filter(
                    ([action]) => action !== "new-notebook" || notebookChat !== null,
                  ).map(([action, label]) => (
                    <button
                      key={action}
                      type="button"
                      className="alk-files-browser__create"
                      disabled={leasedHere}
                      aria-disabled={leasedHere || undefined}
                      title={leasedHere ? LEASED_HERE : undefined}
                      onClick={() => acts.run(action)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              ))
        }
        driveId={driveId}
        parentId={listFolderId}
        chain={chain}
        up={{
          title: upTitle,
          go: canGoUp ? openParent : undefined,
        }}
        filters={listFilters}
        onNavigate={openNode}
        onOpen={openItem}
        // A link to a file puts the reader in front of that row: it is selected
        // on its own and scrolled to, so closing the preview leaves them where
        // the file is rather than at the top of its folder. A reference inside a
        // previewed document is the same question asked from inside the sheet,
        // so it is answered on the same channel.
        revealId={revealedId ?? (landing === "file-in-folder" ? landedId : null)}
        onDropToSegment={(id, event) => dropOn(id, event, acts)}
        dragDrop={dragDropFor(acts)}
        notice={
          <>
            {objectNotice === null ? null : (
              <p className="alk-files-browser__notice">{objectNotice}</p>
            )}
            {/* What the rows under it ARE: this second's view of a machine that
                is writing them, or the copy that last reached storage. It also
                carries which chat holds the folder and the way there, because
                that is the same fact and a reader only needs it once. Drawn
                only for a folder that actually has a lease — an ordinary folder
                is not "last saved", it just is. */}
            {folder.data?.lease ? (
              <LiveBadge
                liveness={liveness.liveness}
                streamDown={liveness.streamDown}
                chat={leasingChat}
                inChat={dressedAsChat !== undefined && isChatFolder(dressedAsChat)}
              />
            ) : null}
          </>
        }
        omit={chatRecordsRule(folder.data)}
        footer={footer}
        renderNameOverride={renderNameOverride}
        rowAdornment={rowAdornment}
        onSelectionChange={setSelectedIds}
        orderBy={filters.orderBy}
        onSort={onSort}
        resumeFocus={resumeFocus}
        onFocusResumed={onFocusResumed}
        platform={platformHere}
      />
    );

  // Off the file the drive called home, and off the index that would route back to
  // it: the root listing is where the rail's places are, so it is where the page
  // lands when the only home it was given cannot be browsed.
  const bounceToRoot =
    unbrowsableHomeId !== null &&
    rootId !== undefined &&
    rootId !== "" &&
    (nodeId === unbrowsableHomeId || (!trash && nodeId === undefined));
  if (bounceToRoot) {
    return <Navigate to={`/files/${rootId}`} replace />;
  }
  // A deep link to the folder-object itself lands on its files — the working
  // directory is the effective root of a chat or a template, and the folder
  // around it holds only the object's own records. Replaced rather than pushed,
  // so Back does not bounce between the two.
  const objectFiles = isFolderObject(node.data) ? browseTargetOf(node.data as Item) : null;
  if (objectFiles !== null && objectFiles !== nodeId) {
    return <Navigate to={`/files/${objectFiles}`} replace />;
  }

  return (
    <FilesHomeProvider nodeId={homeNodeId}>
      <FilesActions
        driveId={driveId}
        currentFolderId={listFolderId}
        currentTrail={chain}
        currentFolderName={
          dressedAsChat
            ? displayNameOf(dressedAsChat)
            : folder.data
              ? displayNameOf(folder.data)
              : undefined
        }
        selection={selection}
        platform={platformHere}
        inTrash={trash}
        canWriteHere={(folder.data?.capabilities?.can_write ?? false) && !onSignpost}
        leasedHere={leasedHere}
        isHeld={isHeld}
        // Taking a folder back from its holder is a manager's write, and the
        // server says on the row itself who may make it — so the menu reads the
        // capability rather than guessing from a role the page does not have.
        canForceRelease={canLease(active, "lease_force")}
        onOpen={openItem}
        onViewFiles={viewChatFiles}
        onOpenPage={openObjectPage}
        onOpenParent={openParent}
        onRename={(item) => setRenamingId(item.id)}
        onQuickLook={() => paneRef.current?.focus()}
        onDetails={() => paneRef.current?.focus()}
        onStartChat={newChatFromTemplate}
        onNewChatFromTemplate={newChatFromTemplate}
        onUndo={() => void undoStack.undo()}
        onRedo={() => void undoStack.redo()}
        onNewFolder={() => {
          setCreateRefusal(null);
          setCreateDraft("");
          setNaming("folder");
        }}
        onNewNotebook={
          notebookChat === null
            ? undefined
            : () => {
                setCreateRefusal(null);
                setCreateDraft(DEFAULT_NOTEBOOK_NAME);
                setNaming("notebook");
              }
        }
        onUploadFiles={() => filePicker.current?.click()}
        onUploadFolder={() => folderPicker.current?.click()}
        onOperation={(operation) => {
          // What the write earns as history — the opposite move for one the
          // server ran inline, the operation's own inverse for the rest, and
          // nothing at all for a write the server cannot take back.
          const step = undoStep(operation);
          if (step) undoStack.push(step);
        }}
      >
        {(api) => {
          // The one refusal line, borrowed for the decisions the page makes
          // before any request is sent.
          refuseHere.current = api.refuse;
          return (
            <FilesPage
              trash={trash}
              place={place}
              toolbar={toolbar}
              breadcrumb={<LeaseBadge item={folder.data} variant="breadcrumb" />}
              pane={renderPane(api)}
              placeNodeIds={placeNodeIds}
              virtualPlaces={VIRTUAL_PLACES}
              driveReady={driveId !== undefined}
              resolveError={resolveError}
              onRetryResolve={retryResolve}
              onSelectPlace={(next) => {
                // A node-addressed place is routed by the shell, and the route it
                // goes to carries no query at all — so only a feed has anything to
                // record here.
                const virtual = virtualPlaceFrom(next);
                if (virtual) showPlace(virtual);
              }}
              browser={
                <div
                  className="alk-files__drop"
                  ref={browserRef}
                  tabIndex={-1}
                  onKeyDown={api.onKeyDown}
                  onContextMenu={api.triggerProps.onContextMenu}
                  {...listingProps(api)}
                >
                  {listingDropActive ? (
                    <div className="alk-files__drop-hint" aria-hidden="true">
                      Drop to upload into {hereName}
                    </div>
                  ) : null}
                  {/* The New-folder form lives in the browser bar (the `actions` slot), where the
                      buttons were, so opening it never pushes the trail and the rows down. */}
                  {/* The two pickers the menu's Upload items open. Hidden rather
                      than absent, because a click must reach a real input for the
                      browser to show a file dialog at all. */}
                  <input
                    ref={filePicker}
                    type="file"
                    multiple
                    hidden
                    aria-label="Upload files"
                    onChange={(event) => {
                      void uploads.onDrop(
                        dropTargetOf(folder.data, leasedHere),
                        pickedTransfer(Array.from(event.target.files ?? [])),
                      );
                      event.target.value = "";
                    }}
                  />
                  <input
                    ref={folderPicker}
                    type="file"
                    hidden
                    aria-label="Upload folder"
                    {...{ webkitdirectory: "" }}
                    onChange={(event) => {
                      void uploads.onDrop(
                        dropTargetOf(folder.data, leasedHere),
                        pickedTransfer(Array.from(event.target.files ?? [])),
                      );
                      event.target.value = "";
                    }}
                  />
                  {renderBrowserBody(api)}
                  {/* Pinned under the rows: what is selected, what it weighs,
                      and the actions that act on all of it — read off the menu
                      the same click would open, never decided again here. */}
                  <SelectionBar
                    selection={selection}
                    menuItems={api.menuItems}
                    {...(browserDrawn ? { scope: selectionScope, onSelectRest: selectRest } : {})}
                  />
                  {driveId !== undefined ? (
                    <FilePreviewModal
                      driveId={driveId}
                      item={preview.item}
                      open={preview.open}
                      onClose={closePreview}
                      onOpenInFiles={
                        // A file shared alone has nowhere to be shown: its folder
                        // is not the reader's, so the key that walks into it is
                        // not offered rather than offered and refused.
                        landing !== "file-solo" && preview.item?.parentId
                          ? () => {
                              const parent = preview.item?.parentId;
                              preview.close();
                              if (parent) openNode(parent);
                            }
                          : undefined
                      }
                      onPrev={preview.onPrev}
                      onNext={preview.onNext}
                      resolver={previewResolver}
                    />
                  ) : null}
                  <div className="alk-files-tray">
                    <UploadTray
                      rows={uploads.rows}
                      skippedSidecars={uploads.skippedSidecars}
                      identicalCopies={uploads.identicalCopies}
                      alreadyInFiles={uploads.alreadyInFiles}
                      finished={uploads.finished}
                      refusal={uploads.refusal}
                      batch={uploads.batch}
                      conflicts={uploads.conflicts}
                      resumable={uploads.resumable}
                      onPause={uploads.pause}
                      onResume={uploads.resume}
                      onCancel={uploads.cancel}
                      onAnswerConflict={uploads.answerConflict}
                      onRetry={(id) => void uploads.retry(id)}
                      onRetryFailed={() => void uploads.retryFailed()}
                      onDismiss={uploads.dismiss}
                    />
                  </div>
                  <UndoToast controller={undoStack} />
                </div>
              }
            />
          );
        }}
      </FilesActions>
    </FilesHomeProvider>
  );
}

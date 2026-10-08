/**
 * The browser: a folder's children in the treegrid, the view switch, the sort, and the
 * breadcrumb trail.
 *
 * It owns the selection and the view. The order is *controlled* whenever an `orderBy`
 * prop is handed in: the page keeps it in the URL so the grid and the soft-threshold
 * footer read the same listing, and a sorted view is linkable. Without the prop the
 * browser falls back to its own state, which is what an isolated render wants. Either
 * way the order is a request, not a local re-sort — a header click is a *new server
 * read*, not a shuffle of the page already on screen. The rows come from `useChildren`
 * (one keyset page at a time), and every mutation the rows need belongs to the hooks in
 * `api/files.ts`.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  flattenChildren,
  useChildren,
  type ChildrenOptions,
  type Item,
  type ListFilters,
  type OrderBy,
  type OrderDirection,
  type OrderField,
} from "@/api/files";
import { Icon } from "@/app/icons";
import { Breadcrumbs, type BreadcrumbsUp, type Crumb } from "./Breadcrumbs";
import { leftElement } from "./dragDrop";
import { Treegrid, type FilesView, type TreegridDragDrop } from "./Treegrid";
import {
  emptySelection,
  reconcileSelection,
  selectionReducer,
  type SelectionAction,
} from "./state/selection";
import type { Platform } from "./state/shortcuts";
import { entryVisibility, useShowHiddenFiles, visibleRows } from "./hiddenEntries";
import { safeLocalStorage } from "@alkera/ui/storage";

/** The page's side of a drag: what a picked-up row carries, which rows may be
 *  landed on, and what a drop does. The browser owns only the highlight — which
 *  row or segment is under the pointer — so the trail and the grid paint one
 *  target between them and the page never has to track a pointer. */
export interface BrowserDragDrop {
  onDragStart: (item: Item, event: React.DragEvent<HTMLElement>) => void;
  onDragEnd?: (event: React.DragEvent<HTMLElement>) => void;
  /** Whether a drag may land on this row. */
  droppable: (item: Item) => boolean;
  /** A drop on a folder row. */
  onDropOnRow: (item: Item, event: React.DragEvent<HTMLElement>) => void;
}

export const DEFAULT_ORDER: OrderBy = { field: "name", direction: "asc" };

/** One key per drive: a person's grid preference for their photos folder should not
 *  follow them into a drive full of code. */
export function viewStorageKey(driveId: string | undefined): string {
  return `alkera.files.view.${driveId ?? "unknown"}`;
}

/** Storage is a convenience, never a dependency: a private window, a blocked-cookie
 *  browser and a thumbnailer all throw on access, and the browser must still render.
 *  The guarded store (`@alkera/ui/storage`) is what makes that true here. */
export function readStoredView(driveId: string | undefined): FilesView | null {
  const stored = safeLocalStorage().get(viewStorageKey(driveId));
  return stored === "list" || stored === "grid" ? stored : null;
}

export function writeStoredView(driveId: string | undefined, view: FilesView): void {
  // The view still switches; on a browser that refuses the write it just does
  // not survive a reload.
  safeLocalStorage().set(viewStorageKey(driveId), view);
}

export interface FilesBrowserProps {
  driveId: string | undefined;
  /** The folder being listed. */
  parentId: string | undefined;
  /** Root first, current folder last. */
  chain?: readonly Crumb[];
  /** The way up, driven by the folder's own parent so it works on a deep link
   *  whose trail has one segment. Absent, the trail carries no such control. */
  up?: BreadcrumbsUp;
  filters?: ListFilters;
  onNavigate?: (nodeId: string) => void;
  onOpen?: (item: Item) => void;
  /** A plain single click on a file row (see `Treegrid`). */
  onPreview?: (item: Item) => void;
  /** Put the tab stop back on the first row when this listing's rows arrive —
   *  the keyboard walked in here, and the rows it walked from are gone. */
  resumeFocus?: boolean;
  onFocusResumed?: () => void;
  /** Handed straight to the breadcrumb trail by the drag-and-drop layer. */
  onDropToSegment?: BreadcrumbsDrop;
  /** Rows as drag sources and folder rows as drop targets. */
  dragDrop?: BrowserDragDrop;
  /** Rendered inside the rowgroup after the last row — the soft-threshold footer. */
  footer?: React.ReactNode;
  /** Rendered above the rows: what this listing is, when it is not obvious. */
  notice?: React.ReactNode;
  /** Rows this listing does not show, on top of the system entries the browser
   *  hides unless the viewer asks to see them. The listing is still the
   *  server's — the predicate only decides what a person is shown, which is how
   *  a chat's own runtime directory stays out of the working material it sits
   *  beside. "Show hidden files" does not bring these back. */
  omit?: (item: Item) => boolean;
  /** Replaces a row's name cell while it is being renamed inline. */
  renderNameOverride?: (item: Item) => React.ReactNode | null;
  /** Rendered beside a row's name: what that row is doing on the machine
   *  holding the folder. Every row is offered it, and a row with nothing to say
   *  renders nothing — so a listing can hand this in unconditionally. */
  rowAdornment?: (item: Item) => React.ReactNode;
  /** Called whenever the selection changes, so the page can drive the right pane. */
  onSelectionChange?: (ids: readonly string[]) => void;
  /** The requested order. Supplying it makes the sort controlled: the browser asks the
   *  server for THIS order and reports a header click through `onSort` instead of
   *  re-ordering behind the caller's back. Omit both to let the browser own the order. */
  orderBy?: OrderBy;
  /** A header click, when the sort is controlled. */
  onSort?: (field: OrderField, direction: OrderDirection) => void;
  readOptions?: ChildrenOptions;
  platform?: Platform;
  /** Rendered in the bar beside the View group — the page's create and upload
   *  buttons, which need the action runner the page owns. */
  actions?: React.ReactNode;
  /** One row to put the reader in front of, selected on its own and scrolled to.
   *  It comes from outside the listing — a file named in the transcript, a file
   *  the machine just wrote — so it is a request, not a selection the reader made. */
  revealId?: string | null;
  initialRect?: { width: number; height: number };
}

type BreadcrumbsDrop = NonNullable<React.ComponentProps<typeof Breadcrumbs>["onDropToSegment"]>;

export function FilesBrowser({
  driveId,
  parentId,
  up,
  chain = [],
  filters,
  onNavigate,
  onOpen,
  onPreview,
  resumeFocus,
  onFocusResumed,
  onDropToSegment,
  dragDrop,
  footer,
  notice,
  omit,
  renderNameOverride,
  rowAdornment,
  onSelectionChange,
  orderBy: controlledOrderBy,
  onSort: onSortProp,
  readOptions,
  platform,
  actions,
  revealId = null,
  initialRect,
}: FilesBrowserProps) {
  // The one target under the pointer, row or segment. Cleared when the pointer
  // leaves it for outside (not for one of its own cells), when the drop lands,
  // and when the drag ends anywhere — a drag cancelled with Escape fires no
  // leave on the element it was over.
  const [activeDropId, setActiveDropId] = useState<string | null>(null);
  useEffect(() => {
    if (activeDropId === null) return undefined;
    const clear = () => setActiveDropId(null);
    window.addEventListener("dragend", clear);
    window.addEventListener("drop", clear);
    return () => {
      window.removeEventListener("dragend", clear);
      window.removeEventListener("drop", clear);
    };
  }, [activeDropId]);

  const gridDragDrop = useMemo<TreegridDragDrop | undefined>(() => {
    if (!dragDrop) return undefined;
    return {
      onDragStart: dragDrop.onDragStart,
      onDragEnd: dragDrop.onDragEnd,
      droppable: dragDrop.droppable,
      onDragOver: (item) => setActiveDropId((current) => (current === item.id ? current : item.id)),
      onDragLeave: (item, event) => {
        if (leftElement(event)) setActiveDropId((current) => (current === item.id ? null : current));
      },
      onDrop: (item, event) => {
        setActiveDropId(null);
        dragDrop.onDropOnRow(item, event);
      },
    };
  }, [dragDrop]);
  const [localOrderBy, setLocalOrderBy] = useState<OrderBy>(DEFAULT_ORDER);
  const orderBy = controlledOrderBy ?? localOrderBy;
  const [view, setView] = useState<FilesView>("list");
  // The drive id arrives a render late — the page derives it from the drive query —
  // so the stored preference is adopted when the id becomes known. Reading it once
  // at mount would read the `undefined` key, which nothing ever writes. Before the
  // frame, so a reader who chose the grid is not shown a list first.
  useLayoutEffect(() => {
    if (driveId === undefined) return;
    // A drive with no stored preference starts at the list rather than inheriting
    // the one before it: the grid chosen for a photo drive must not follow the
    // person into a drive full of code.
    setView(readStoredView(driveId) ?? "list");
  }, [driveId]);
  const [selection, setSelection] = useState(emptySelection);

  const children = useChildren(driveId, parentId, { ...readOptions, filters, orderBy });
  const listed = useMemo(() => flattenChildren(children.data), [children.data]);
  const [showHidden, setShowHidden] = useShowHiddenFiles();
  const rows = useMemo(() => visibleRows(listed, omit, showHidden), [listed, omit, showHidden]);
  const dimmed = useCallback(
    (item: Item) => entryVisibility(item, omit, showHidden) === "dimmed",
    [omit, showHidden],
  );

  // Rows that leave carry the focus and the selection with them: a trashed row
  // hands both to its neighbour rather than leaving the tab stop on a row that
  // is not there. The order before the change is the only place the neighbour
  // can be read from, so it is kept across the render that removes it.
  // A listing the grid has an answer for. Changing the order is a NEW query, and
  // for the render between the header click and its answer there is no data at
  // all — the grid paints nothing. Reconciling against that frame read as "every
  // row has left the folder" and cleared the whole selection along with the tab
  // stop, so a sort silently dropped 25 selected rows and left the keyboard with
  // nowhere to sit. The rows are not gone; they are on their way.
  const hasListing = children.data !== undefined;
  const previousOrder = useRef<readonly string[]>([]);
  useEffect(() => {
    if (!hasListing) return;
    const order = rows.map((row) => row.id);
    const before = previousOrder.current;
    previousOrder.current = order;
    if (before.length === 0) return;
    setSelection((current) => reconcileSelection(current, before, order));
  }, [rows, hasListing]);

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

  // Telling the page what is selected is a side effect, so it happens after the
  // commit, not inside the updater above: React runs an updater during the
  // RENDER phase, and a parent setState from there is the "cannot update a
  // component while rendering a different component" error — which a right-click
  // (the Share… path) hit on every open. Held in a ref so an inline handler
  // cannot turn the effect into a render loop, and skipped while the selected
  // set is the very same one, so the mount pass says nothing.
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

  const onSort = useCallback(
    (field: OrderField, direction: OrderDirection) => {
      if (onSortProp) onSortProp(field, direction);
      else setLocalOrderBy({ field, direction });
    },
    [onSortProp],
  );

  const chooseView = useCallback(
    (next: FilesView) => {
      setView(next);
      writeStoredView(driveId, next);
    },
    [driveId],
  );

  return (
    <div className="alk-files-browser">
      <div className="alk-files-browser__bar">
        <Breadcrumbs
          segments={chain}
          up={up}
          onNavigate={onNavigate}
          onDropToSegment={
            onDropToSegment
              ? (id, event) => {
                  setActiveDropId(null);
                  onDropToSegment(id, event);
                }
              : undefined
          }
          onDragOverSegment={(id) =>
            setActiveDropId((current) => (current === id ? current : id))
          }
          onDragLeaveSegment={(id, event) => {
            if (leftElement(event)) setActiveDropId((current) => (current === id ? null : current));
          }}
          activeDropId={activeDropId}
        />
        {actions}
        <div className="alk-files-browser__views" role="group" aria-label="View">
          {(["list", "grid"] as const).map((option) => (
            <button
              key={option}
              type="button"
              className="alk-files-browser__view"
              aria-pressed={view === option}
              onClick={() => chooseView(option)}
            >
              {option === "list" ? "List" : "Grid"}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="alk-files-browser__view alk-files-browser__hidden-toggle"
          aria-pressed={showHidden}
          aria-label="Show hidden files"
          title="Show hidden files"
          onClick={() => setShowHidden(!showHidden)}
        >
          <Icon name={showHidden ? "eye" : "eyeoff"} size={14} />
        </button>
      </div>
      {notice}
      <Treegrid
        rows={rows}
        view={view}
        selection={selection}
        onSelectionAction={onSelectionAction}
        orderBy={orderBy}
        onSort={onSort}
        onOpen={onOpen}
        {...(onPreview ? { onPreview } : {})}
        {...(resumeFocus === undefined ? {} : { resumeFocus })}
        {...(onFocusResumed ? { onFocusResumed } : {})}
        dragDrop={gridDragDrop}
        activeDropId={activeDropId}
        footer={footer}
        renderNameOverride={renderNameOverride}
        rowAdornment={rowAdornment}
        dimmed={showHidden ? dimmed : undefined}
        platform={platform}
        revealId={revealId}
        initialRect={initialRect}
      />
    </div>
  );
}

export default FilesBrowser;

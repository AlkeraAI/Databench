/**
 * The Files treegrid: one ARIA `treegrid` that backs both the list and the grid view.
 *
 * Both views render the same rows in the same order from the same selection state, so
 * switching views can never change what is selected or what is focused — only how a row
 * is drawn. The keyboard, the roving tabindex and the range selection all come from the
 * page's pure reducers (`state/selection.ts`, `state/shortcuts.ts`); this component owns
 * only the DOM and the virtualization.
 *
 * Virtualization is `@tanstack/react-virtual` (headless — it renders nothing itself), so
 * a folder with 100k children scrolls without 100k rows in the document. The list
 * virtualizes single rows; the grid virtualizes *lanes* of tiles, whose width is measured
 * from the scroller, because a lane is what a grid's `role="row"` actually is.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronDownIcon, HomeIcon, LanguageIcon } from "@alkera/ui";

import type { Item, OrderBy, OrderField } from "@/api/files";
import { Icon } from "@/app/icons";
import {
  ariaSortFor,
  gridTemplateFor,
  visibleColumns,
  displayNameOf,
  formatModified,
  formatSize,
  iconHintFor,
  isHome,
  navIconFor,
  kindLabel,
  locationOf,
  modifiedOf,
  nextDirection,
  orderFieldFor,
  sizeOf,
  type FilesColumn,
} from "@/lib/files/columns";
import { OwnerCell } from "./OwnerCell";
import {
  describeSelection,
  selectionReducer,
  type FocusKey,
  type SelectionAction,
  type SelectionState,
} from "./state/selection";
import { detectPlatform, type Platform } from "@/lib/platform";
import { resolveShortcut } from "./state/shortcuts";
import "./treegrid.css";

export type FilesView = "list" | "grid";

/** Row height in the list, and lane height in the grid. Kept in one place because the
 *  virtualizer estimates with it and the CSS lays out with it. */
export const ROW_HEIGHT = 32;
export const TILE_HEIGHT = 120;
/** The narrowest a tile may be before the lane drops a column. */
export const TILE_MIN_WIDTH = 160;
/** The lane's gap, and the padding on each of its sides — both `--alkSpace2` in
 *  `treegrid.css`, pinned against the token by `gridTiling.test.tsx`. A lane of N tiles is
 *  N tracks plus N-1 gaps plus two paddings, so a count taken from the raw width alone
 *  claims room the lane does not have. */
export const TILE_GAP = 6;

/** How many tiles fit across a lane of this width, gaps and the lane's own padding
 *  included. This is the ONLY column count in the grid: the tiles are sliced with it and
 *  the lane's tracks are spelled from it, so a lane can never be handed more tiles than it
  *  has cells. A second count (say, CSS `auto-fill`) would disagree by one near every
 *  breakpoint, and the extra tile would wrap on top of the next lane. */
export function columnsFor(width: number): number {
  const inner = width - TILE_GAP * 2;
  return Math.max(1, Math.floor((inner + TILE_GAP) / (TILE_MIN_WIDTH + TILE_GAP)) || 1);
}

/** A grid lane's tracks, spelled from the count its tiles were sliced with. `minmax(0, …)`
 *  rather than a floor at the tile width: the count already guarantees the room, and a
 *  floor would let a long name grow a track and push the lane past the scroller. */
export function laneTemplateFor(columns: number): string {
  return `repeat(${columns}, minmax(0, 1fr))`;
}

/** What inside a row answers to the pointer itself, so a click there is not also a
 *  click on the row. The row is otherwise the whole target: a listing is mostly
 *  Kind, Size, Modified and Owner, and clicking one of those means the row. */
const ROW_CONTROLS =
  'input,textarea,select,button,a[href],[contenteditable="true"],[role="button"],[role="link"],[role="menuitem"]';

/** The keys that move the roving tabindex, in the reducer's spelling. */
const NAV_KEYS: Readonly<Record<string, FocusKey>> = {
  ArrowDown: "ArrowDown",
  ArrowUp: "ArrowUp",
  Home: "Home",
  End: "End",
};

/** How many tiles fit across a scroller of this width, and how many lanes that many tiles
 *  make. Never zero columns: a viewport too narrow for one tile still shows one, scrolled
 *  sideways, rather than an empty grid. */
export function lanesFor(width: number, count: number): { columns: number; lanes: number } {
  const columns = columnsFor(width);
  return { columns, lanes: Math.ceil(count / columns) };
}

/** The drag layer's hooks into the rows. The grid paints and dispatches; every
 *  decision — what a drag carries, where it may land, what a drop does — is the
 *  caller's, so the list and the grid view can never disagree about one. */
export interface TreegridDragDrop {
  /** A row was picked up. */
  onDragStart: (item: Item, event: React.DragEvent<HTMLElement>) => void;
  onDragEnd?: (event: React.DragEvent<HTMLElement>) => void;
  /** Whether a drag may land on this row at all. Only a folder ever says yes. */
  droppable: (item: Item) => boolean;
  onDragOver: (item: Item, event: React.DragEvent<HTMLElement>) => void;
  onDragLeave: (item: Item, event: React.DragEvent<HTMLElement>) => void;
  onDrop: (item: Item, event: React.DragEvent<HTMLElement>) => void;
}

export interface TreegridProps {
  rows: readonly Item[];
  view: FilesView;
  /** Rows can be picked up and folder rows dropped onto when this is wired. */
  dragDrop?: TreegridDragDrop;
  /** The row the drag layer says is under the pointer, painted as the target. */
  activeDropId?: string | null;
  selection: SelectionState;
  /** The page owns the reducer; the grid only dispatches. */
  onSelectionAction: (action: SelectionAction) => void;
  orderBy: OrderBy;
  /** A header click; the caller re-reads the listing with the new order. */
  onSort: (field: OrderField, direction: "asc" | "desc") => void;
  onOpen?: (item: Item) => void;
  /** A plain single click on a file row: a host that shows files beside the
   *  listing opens a passing preview of it. A double click still opens. */
  onPreview?: (item: Item) => void;
  /** Put the roving tab stop back on the first row once this listing's rows
   *  arrive. Set by a caller whose navigation came from the keyboard: the walk
   *  replaced every row, so the focus was reconciled away and the browser
   *  dropped it out to the document. Cleared through `onFocusResumed`. */
  resumeFocus?: boolean;
  /** The grid has answered `resumeFocus`, one way or the other. */
  onFocusResumed?: () => void;
  /** A row's depth in the tree, for `aria-level`. Flat listings are all level 1. */
  levelOf?: (item: Item) => number;
  /** Rendered inside the rowgroup after the last row (the soft-threshold footer). */
  footer?: React.ReactNode;
  /** Replaces a row's name cell while it is being renamed inline. */
  renderNameOverride?: (item: Item) => React.ReactNode | null;
  /** Rendered after a row's name: what the row itself is doing right now — the
   *  chip a file being written by a machine wears. Separate from the override
   *  above because that one means "this row is being renamed", which takes the
   *  row's drag away; an adornment takes nothing. */
  rowAdornment?: (item: Item) => React.ReactNode;
  /** Rows drawn dimmed: shown only because the viewer asked to see hidden files. */
  dimmed?: (item: Item) => boolean;
  platform?: Platform;
  label?: string;
  /** One row to put the reader in front of: selected on its own, scrolled to and
   *  focused. It arrives from somewhere else entirely — a link in the transcript,
   *  a file the agent just wrote — so the grid cannot wait for the reader to
   *  scroll: the row may not even be mounted when the id lands. */
  revealId?: string | null;
  /** Draw the Location column: which folder each row lives in. A feed asks for it —
   *  its rows come from all over the drive — and a folder's own listing never does,
   *  because every row in one is in the folder the reader is already standing in. */
  showLocation?: boolean;
  /** A click on a Location cell, with the folder's node id. Without it the cell is
   *  the folder's name and nothing to press. */
  onOpenLocation?: (nodeId: string) => void;
  /** Test seam: jsdom measures every element as 0×0, so the virtualizer would show no
   *  rows at all. A caller (and the default) supplies the rect it should assume until a
   *  real measurement arrives. */
  initialRect?: { width: number; height: number };
}

function useMeasuredWidth(
  ref: React.RefObject<HTMLDivElement | null>,
  fallback: number,
): number {
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const read = () => {
      const measured = element.clientWidth;
      setWidth(measured > 0 ? measured : fallback);
    };
    read();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(read);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, fallback]);
  return width;
}

export function Treegrid({
  rows,
  view,
  dragDrop,
  activeDropId = null,
  selection,
  onSelectionAction,
  orderBy,
  onSort,
  onOpen,
  onPreview,
  resumeFocus,
  onFocusResumed,
  levelOf,
  footer,
  renderNameOverride,
  rowAdornment,
  dimmed,
  platform,
  label = "Files",
  showLocation = false,
  onOpenLocation,
  revealId = null,
  initialRect = { width: 960, height: 640 },
}: TreegridProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const resolvedPlatform = useMemo(() => platform ?? detectPlatform(), [platform]);
  const order = useMemo(() => rows.map((row) => row.id), [rows]);
  const width = useMeasuredWidth(scrollRef, initialRect.width);
  // The SCROLLER, not the viewport: the listing is one column of a three-column page, so
  // what a column has to fit in is the width the listing was actually given — which is how
  // Owner fell off the edge of a 1280 px window while the viewport said there was room.
  const shown = useMemo(
    () => visibleColumns(width, { location: showLocation }),
    [width, showLocation],
  );
  const template = useMemo(() => gridTemplateFor(shown), [shown]);
  const { columns, lanes } = useMemo(
    () => (view === "grid" ? lanesFor(width, rows.length) : { columns: 1, lanes: rows.length }),
    [view, width, rows.length],
  );

  const virtualizer = useVirtualizer({
    count: lanes,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => (view === "grid" ? TILE_HEIGHT : ROW_HEIGHT),
    overscan: 8,
    initialRect,
    // A lane's React key is the NODE in it, not the position it is at. Keyed by
    // index — the default — a row trashed above the one a reader is on handed
    // the survivors each other's DOM elements: the browser's focus stayed on an
    // element that now drew a different file, and the inline rename editor was
    // rebuilt under the person typing in it. Both are silent. In the grid a lane
    // holds several tiles, so it is keyed by the first of them; the tiles inside
    // carry their own ids already.
    getItemKey: (index) => (view === "grid" ? rows[index * columns] : rows[index])?.id ?? index,
    // A scroller the browser has not laid out yet measures 0×0, and a virtualizer told
    // its viewport is zero high renders nothing at all. Fall back to the assumed rect so
    // the first paint is a screenful of rows rather than an empty grid, and let the real
    // measurement take over the moment there is one.
    observeElementRect: (instance, cb) => {
      const element = instance.scrollElement as HTMLElement | null;
      if (!element) return;
      const report = () =>
        cb({
          width: element.clientWidth || initialRect.width,
          height: element.clientHeight || initialRect.height,
        });
      report();
      if (typeof ResizeObserver === "undefined") return;
      const observer = new ResizeObserver(report);
      observer.observe(element);
      return () => observer.disconnect();
    },
  });

  const dispatch = useCallback(
    (action: SelectionAction) => onSelectionAction(action),
    [onSelectionAction],
  );

  // A freshly loaded listing has focused nothing yet, and a grid whose every row is
  // `tabIndex -1` cannot be entered from the keyboard at all — Tab skips straight past it
  // and the rows are reachable only by clicking one. The tab stop therefore falls back to
  // the first row; it is a tab stop only, not a focus, so nothing is drawn as focused and
  // no screen-reader caret moves until the user actually arrives.
  const tabbableId = selection.focusedId ?? rows[0]?.id ?? null;

  // The roving tabindex is a real DOM focus: the focused row must actually receive it,
  // or a screen reader follows a caret the browser never moved.
  const focusedId = selection.focusedId;
  // Whether the keyboard was in this grid a moment ago. A removed row takes the
  // browser's focus out to the document with it, so by the time the neighbour
  // mounts the grid no longer contains `activeElement` and the guard below would
  // refuse to follow — which is how ⌘⌫ threw the reader back to the top of the app.
  const heldFocus = useRef(false);
  useEffect(() => {
    if (focusedId === null) return;
    const active = document.activeElement;
    const inGrid = scrollRef.current?.contains(active) === true;
    if (inGrid) heldFocus.current = true;
    // Nothing else has taken the focus: the document itself is holding it, which
    // is where the browser puts it when the element under it is removed. Anything
    // that really took focus — a dialog, a toast's button — keeps it.
    const adrift =
      active === null || active === document.body || active === document.documentElement;
    if (!inGrid && !adrift) {
      heldFocus.current = false;
      return;
    }
    if (!inGrid && !heldFocus.current) return;
    // Focus that is inside the grid but not ON a row belongs to something the
    // person is using — the inline rename editor, a row's own button. Rows moving
    // underneath must not take it away from them.
    if (inGrid && active !== (active as HTMLElement | null)?.closest("[data-row-id]")) return;
    const element = scrollRef.current?.querySelector<HTMLElement>(
      `[data-row-id="${CSS.escape(focusedId)}"]`,
    );
    if (element && active !== element) element.focus();
  }, [focusedId, rows]);

  // Walking into another folder replaces every row, so the focus is reconciled
  // away to nothing and the browser drops it out to the document. The grid then
  // hears no keystroke at all: Cmd+Up, the way back out, was swallowed until
  // somebody clicked a row. The keyboard came in with the walk, so the first row
  // of the new listing takes it back — but only while the focus really is adrift,
  // never off a search field or a dialog that has claimed it since.
  useEffect(() => {
    if (focusedId !== null || rows.length === 0) return;
    if (!heldFocus.current && resumeFocus !== true) return;
    const active = document.activeElement;
    const adrift =
      active === null || active === document.body || active === document.documentElement;
    if (!adrift) {
      // Something else took the focus in the meantime — a search field, a
      // dialog. It keeps it; a listing arriving is not a reason to pull a
      // caret out of a control somebody is typing in.
      heldFocus.current = false;
      onFocusResumed?.();
      return;
    }
    // The keyboard is the grid's again, so the effect above will follow the tab
    // stop onto the row rather than reading the adrift focus as somebody else's.
    heldFocus.current = true;
    // The tab stop only: `preserve` moves the focus and selects nothing, so
    // arriving in a folder never acts as if a row there had been picked.
    dispatch({ type: "focus-move", key: "Home", extend: false, preserve: true });
    onFocusResumed?.();
    // And take the focus here, in the pass that found it adrift, rather than
    // leaving it to the effect above on the render the dispatch schedules. The
    // row the tab stop is about to land on is already the first one and already
    // in the DOM, and anything else re-rendering in the same batch — a toolbar
    // control settling, a sibling's own state — puts another commit between the
    // two, and every keystroke in that gap would reach the document and do nothing.
    const first = rows[0];
    if (first === undefined) return;
    const row = scrollRef.current?.querySelector<HTMLElement>(
      `[data-row-id="${CSS.escape(first.id)}"]`,
    );
    row?.focus();
  }, [focusedId, rows, dispatch, resumeFocus, onFocusResumed]);

  // Putting the reader in front of one row. Two steps, because the grid is
  // virtualized: the row is asked for first (so the virtualizer mounts it), and
  // only once it exists in the DOM is it scrolled to and focused. The second
  // effect deliberately has no dependency list — it is the "did it arrive yet"
  // pass, and it settles the moment there is nothing pending.
  const pendingReveal = useRef<string | null>(null);
  useEffect(() => {
    if (revealId === null || revealId === undefined) return;
    const index = rows.findIndex((row) => row.id === revealId);
    if (index < 0) return;
    // Selected on its own: a reveal answers "this one", not "this one as well".
    dispatch({ type: "click", id: revealId, modifiers: { shift: false, accel: false } });
    pendingReveal.current = revealId;
    virtualizer.scrollToIndex(view === "grid" ? Math.floor(index / columns) : index, {
      align: "center",
    });
  }, [revealId, rows, dispatch, virtualizer, view, columns]);
  useEffect(() => {
    const id = pendingReveal.current;
    if (id === null) return;
    const element = scrollRef.current?.querySelector<HTMLElement>(
      `[data-row-id="${CSS.escape(id)}"]`,
    );
    if (!element) return;
    pendingReveal.current = null;
    element.scrollIntoView({ block: "nearest" });
    element.focus();
  });

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const accel = resolvedPlatform === "mac" ? event.metaKey : event.ctrlKey;
    // Select-all belongs to the grid wherever the keyboard is inside it — and after a
    // header click it is on the header button, because that is what a sort is clicked
    // with. Leaving it to the row shortcuts below meant the keystroke a reader reaches
    // for to check a selection ran as the browser's own Select All over the whole page.
    if (!event.altKey && event.key.toLowerCase() === "a" && (event.metaKey || event.ctrlKey)) {
      // BOTH modifiers are consumed while the grid owns the keystroke. Only this
      // platform's accelerator selects the rows, but letting the other one travel
      // on left the browser to run its own Select All over the document — the nav,
      // the breadcrumb and every button painted in the text-selection highlight.
      event.preventDefault();
      if (accel) dispatch({ type: "select-all" });
      return;
    }
    // The column headers are real buttons living inside the grid, so the rest of the
    // keys are theirs: the row shortcuts must not swallow the Enter that sorts a column
    // (Enter is Open on a row, and preventing it stopped the header button activating).
    if ((event.target as HTMLElement | null)?.closest('[role="columnheader"]') != null) return;
    // Navigation is matched before the shortcut table, because the table's focus rows
    // carry no modifier: Shift+Down is still a move, it just drags the selection with it.
    // Accel+Down is NOT a move — it is Open — so it falls through to the table.
    const nav = NAV_KEYS[event.key];
    if (nav !== undefined && !accel) {
      event.preventDefault();
      dispatch({ type: "focus-move", key: nav, extend: event.shiftKey, preserve: false });
      return;
    }
    const action = resolveShortcut(event, resolvedPlatform);
    if (action === "open") {
      event.preventDefault();
      const item = rows.find((row) => row.id === selection.focusedId);
      if (item && onOpen) onOpen(item);
      return;
    }
    if (action !== null) return;
    // Clearing is selection, not a command, so it is not in the table either.
    if (event.key === "Escape") dispatch({ type: "clear" });
  };

  const onRowClick = (item: Item, event: React.MouseEvent) => {
    // A control drawn inside a row owns its own pointer — the inline rename editor
    // most of all, where a click places a caret and a Shift+click selects text.
    // Reading those as a click on the row underneath threw the selection away
    // mid-rename.
    if ((event.target as HTMLElement | null)?.closest(ROW_CONTROLS) != null) return;
    dispatch({
      type: "click",
      id: item.id,
      modifiers: {
        shift: event.shiftKey,
        accel: resolvedPlatform === "mac" ? event.metaKey : event.ctrlKey,
      },
    });
    // Only the first click of a plain click is a preview: a modified click is a
    // selection gesture, and the second click of a double click is the open.
    const plain = !event.shiftKey && !event.metaKey && !event.ctrlKey && !event.altKey;
    if (onPreview && plain && event.detail <= 1 && item.kind !== "folder") onPreview(item);
  };

  // A right-click acts on the row under the pointer. The menu that is about to open is
  // built from the SELECTION, so a row outside it has to join the selection before the
  // menu reads it — without this a right-click on an unselected row opened a menu whose
  // every capability-gated item was disabled for want of a target. A right-click INSIDE
  // the selection leaves it alone, so "these five" survives the click that acts on them.
  // The event is not consumed: it still bubbles to whatever opens the menu.
  const onRowContextMenu = (item: Item) => {
    if (selection.selected.has(item.id)) return;
    dispatch({ type: "click", id: item.id, modifiers: { shift: false, accel: false } });
  };

  // The attributes that make a row a drag source and, for a folder, a drop
  // target. Spread on the lane in the list (the row IS the lane there) and on
  // each tile in the grid, so both views drag and drop the same way. A row
  // being renamed is not draggable: a `draggable` ancestor steals the pointer
  // from the text selection inside its input.
  const dragPropsFor = (item: Item, renaming: boolean) => {
    if (!dragDrop) return {};
    const droppable = dragDrop.droppable(item);
    return {
      draggable: !renaming,
      onDragStart: (event: React.DragEvent<HTMLElement>) => dragDrop.onDragStart(item, event),
      onDragEnd: dragDrop.onDragEnd,
      "data-drop-target": droppable ? "folder" : undefined,
      "data-drop-active": droppable && activeDropId === item.id ? "true" : undefined,
      onDragEnter: droppable
        ? (event: React.DragEvent<HTMLElement>) => dragDrop.onDragOver(item, event)
        : undefined,
      onDragOver: droppable
        ? (event: React.DragEvent<HTMLElement>) => dragDrop.onDragOver(item, event)
        : undefined,
      onDragLeave: droppable
        ? (event: React.DragEvent<HTMLElement>) => dragDrop.onDragLeave(item, event)
        : undefined,
      onDrop: droppable
        ? (event: React.DragEvent<HTMLElement>) => dragDrop.onDrop(item, event)
        : undefined,
    };
  };

  const virtualItems = virtualizer.getVirtualItems();

  return (
    <div className="alk-files-grid" data-view={view} data-columns={shown.length}>
      <div aria-live="polite" className="alk-files-grid__status">
        {describeSelection(selection)}
      </div>
      <div
        className="alk-files-grid__scroll"
        ref={scrollRef}
        role="treegrid"
        aria-label={label}
        aria-rowcount={rows.length}
        aria-colcount={view === "grid" ? columns : shown.length}
        aria-multiselectable="true"
        // With no rows there is no row to carry the tab stop, so the grid itself takes it:
        // Tab lands on something the screen reader announces ("Files, tree grid") instead
        // of skipping the region entirely. Once a row exists it owns the only stop.
        tabIndex={rows.length === 0 ? 0 : undefined}
        onKeyDown={onKeyDown}
      >
        {view === "list" ? (
          <div className="alk-files-grid__head" role="rowgroup">
            <div
              className="alk-files-grid__row"
              role="row"
              aria-rowindex={1}
              style={{ gridTemplateColumns: template }}
            >
              {shown.map((column, index) => (
                <HeaderCell
                  key={column.id}
                  column={column}
                  index={index + 1}
                  orderBy={orderBy}
                  onSort={onSort}
                />
              ))}
            </div>
          </div>
        ) : null}
        <div
          className="alk-files-grid__body"
          role="rowgroup"
          style={{ height: `${virtualizer.getTotalSize()}px` }}
        >
          {virtualItems.map((virtual) => {
            const start = view === "grid" ? virtual.index * columns : virtual.index;
            const lane = rows.slice(start, start + columns);
            if (lane.length === 0) return null;
            const listRow = view === "grid" ? null : lane[0]!;
            const listOverride = listRow ? (renderNameOverride?.(listRow) ?? null) : null;
            return (
              <div
                key={virtual.key}
                className="alk-files-grid__lane"
                role="row"
                aria-level={levelOf ? levelOf(lane[0]!) : 1}
                aria-rowindex={virtual.index + 2}
                data-lane={view === "grid" ? "grid" : "list"}
                style={{
                  transform: `translateY(${virtual.start}px)`,
                  height: `${virtual.size}px`,
                  // Both views spell their tracks here, from the same numbers the cells
                  // were sliced with. A grid lane is one row high and absolutely
                  // positioned, so a track count the CSS worked out for itself could
                  // hold one cell fewer than the lane had tiles, and the overflowing
                  // tile wrapped down on top of the next lane.
                  gridTemplateColumns: view === "list" ? template : laneTemplateFor(columns),
                }}
                // In the list a lane IS one row, so the row's identity, its selected
                // state and its tab stop live here — on the `role="row"` element a
                // screen reader driving a treegrid reads them from — and the whole
                // lane is what the pointer acts on. A grid lane holds several items
                // instead, so there each TILE carries its own; a lane of four cannot
                // be "selected".
                {...(listRow
                  ? rowAttributes(
                      listRow,
                      selection.selected.has(listRow.id),
                      tabbableId === listRow.id,
                      dimmed?.(listRow) === true,
                    )
                  : {})}
                {...(listRow ? dragPropsFor(listRow, listOverride !== null) : {})}
                {...(listRow
                  ? {
                      onClick: (event: React.MouseEvent) => onRowClick(listRow, event),
                      onContextMenu: () => onRowContextMenu(listRow),
                      ...(onOpen ? { onDoubleClick: () => onOpen(listRow) } : {}),
                    }
                  : {})}
              >
                {view === "grid"
                  ? lane.map((item) => {
                      const nameOverride = renderNameOverride?.(item) ?? null;
                      return (
                        <Tile
                          key={item.id}
                          item={item}
                          selected={selection.selected.has(item.id)}
                          tabbable={tabbableId === item.id}
                          hiddenEntry={dimmed?.(item) === true}
                          order={order}
                          onClick={onRowClick}
                          onRowContextMenu={onRowContextMenu}
                          onOpen={onOpen}
                          nameOverride={nameOverride}
                          adornment={rowAdornment?.(item) ?? null}
                          dragProps={dragPropsFor(item, nameOverride !== null)}
                        />
                      );
                    })
                  : (
                      <ListCells
                        item={listRow!}
                        columns={shown}
                        nameOverride={listOverride}
                        adornment={rowAdornment?.(listRow!) ?? null}
                        {...(onOpenLocation ? { onOpenLocation } : {})}
                      />
                    )}
              </div>
            );
          })}
        </div>
        {footer}
      </div>
    </div>
  );
}

function HeaderCell({
  column,
  index,
  orderBy,
  onSort,
}: {
  column: FilesColumn;
  index: number;
  orderBy: OrderBy;
  onSort: (field: OrderField, direction: "asc" | "desc") => void;
}) {
  const field = orderFieldFor(column);
  const sort = ariaSortFor(column, orderBy);
  return (
    <div
      className="alk-files-grid__cell alk-files-grid__cell--head"
      role="columnheader"
      aria-colindex={index}
      aria-sort={sort}
      data-column={column.id}
      data-numeric={column.numeric ? "true" : undefined}
    >
      {field === undefined ? (
        column.label
      ) : (
        // The name says what the click DOES; the arrow beside the label is decoration
        // for the eye, so it is hidden and the direction is read off `aria-sort`.
        <button
          type="button"
          className="alk-files-grid__sort"
          aria-label={`Sort by ${column.label}`}
          onClick={() => onSort(field, nextDirection(field, orderBy))}
        >
          <span className="alk-files-grid__sort-label">{column.label}</span>
          {sort === "none" ? null : (
            <span
              className="alk-files-grid__sort-arrow"
              data-sort-arrow=""
              data-direction={sort === "descending" ? "desc" : "asc"}
              aria-hidden
            >
              <ChevronDownIcon size={12} />
            </span>
          )}
        </button>
      )}
    </div>
  );
}

interface CellProps {
  item: Item;
  selected: boolean;
  /** Carries the roving tab stop. Falls back to the first row when nothing is focused. */
  tabbable: boolean;
  /** Shown only because the viewer asked to see hidden files. */
  hiddenEntry?: boolean;
  onClick: (item: Item, event: React.MouseEvent) => void;
  /** Right-click on the row, before the menu that reads the selection opens. */
  onRowContextMenu: (item: Item) => void;
  onOpen?: (item: Item) => void;
  nameOverride: React.ReactNode | null;
  /** Rendered beside the name — what this row is doing on a machine. */
  adornment: React.ReactNode;
}

/** The one place a row's identity is written into the DOM, so both views expose the same
 *  hooks to the keyboard, to the drag layer and to a test. */
function rowAttributes(item: Item, selected: boolean, tabbable: boolean, hiddenEntry = false) {
  return {
    "data-row-id": item.id,
    "data-kind": item.kind,
    "data-hidden-entry": hiddenEntry ? "true" : undefined,
    "aria-selected": selected,
    tabIndex: tabbable ? 0 : -1,
  } as const;
}

/** A row's mark: the portal's own glyph when the row IS something the portal has a
 *  page for, the shared file sprite otherwise. A chat is a `.alkerachat` FOLDER, so
 *  without this it wore the generic folder glyph of the directory it only resembles. */
function RowIcon({ item, size }: { item: Item; size?: number }) {
  if (isHome(item)) {
    // A member's home wears the home mark beside its owner's name, never a folder
    // glyph beside the id it is stored under.
    return (
      <span className="alk-language-icon" aria-hidden data-home="true">
        <HomeIcon size={size ?? 16} />
      </span>
    );
  }
  const nav = navIconFor(item);
  if (nav !== null) {
    return (
      <span className="alk-language-icon" aria-hidden>
        <Icon name={nav} size={size ?? 16} />
      </span>
    );
  }
  return <LanguageIcon path={iconHintFor(item)} size={size} />;
}

/** Where a row lives: the folder's name, and a press that opens it.
 *
 *  A row the server named no folder for draws nothing — not a dash. In a feed the
 *  empty cell means "you hold this file and nothing above it", and an em-dash there
 *  reads as a folder whose name is a dash. */
function LocationCell({
  item,
  onOpenLocation,
}: {
  item: Item;
  onOpenLocation?: (nodeId: string) => void;
}) {
  const location = locationOf(item);
  if (location === null) return null;
  const label = (
    <>
      {location.home ? <HomeIcon size={14} aria-hidden data-home="true" /> : null}
      {location.name}
    </>
  );
  if (onOpenLocation === undefined) return label;
  return (
    <button
      type="button"
      className="alk-files-grid__location"
      title={location.name}
      onClick={() => onOpenLocation(location.id)}
    >
      {label}
    </button>
  );
}

function ListCells({
  item,
  columns,
  nameOverride,
  adornment,
  onOpenLocation,
}: Pick<CellProps, "item" | "nameOverride" | "adornment"> & {
  columns: readonly FilesColumn[];
  onOpenLocation?: (nodeId: string) => void;
}) {
  const modified = modifiedOf(item);
  const content: Readonly<Record<FilesColumn["id"], React.ReactNode>> = {
    name: nameOverride ?? (
      <>
        <RowIcon item={item} />
        <span className="alk-files-grid__name">{displayNameOf(item)}</span>
        {adornment}
      </>
    ),
    kind: kindLabel(item),
    size: formatSize(sizeOf(item)),
    modified: formatModified(modified),
    owner: <OwnerCell item={item} />,
    location: <LocationCell item={item} {...(onOpenLocation ? { onOpenLocation } : {})} />,
  };
  return (
    <>
      {columns.map((column, index) => (
        <div
          key={column.id}
          className="alk-files-grid__cell"
          role="gridcell"
          aria-colindex={index + 1}
          data-column={column.id}
          data-numeric={column.numeric ? "true" : undefined}
        >
          {content[column.id]}
        </div>
      ))}
    </>
  );
}

function Tile({
  item,
  selected,
  tabbable,
  hiddenEntry = false,
  onClick,
  onRowContextMenu,
  onOpen,
  nameOverride,
  adornment,
  dragProps,
}: CellProps & { order: readonly string[]; dragProps: React.HTMLAttributes<HTMLDivElement> }) {
  return (
    <div
      className="alk-files-grid__tile"
      role="gridcell"
      {...rowAttributes(item, selected, tabbable, hiddenEntry)}
      {...dragProps}
      onClick={(event) => onClick(item, event)}
      onContextMenu={() => onRowContextMenu(item)}
      onDoubleClick={onOpen ? () => onOpen(item) : undefined}
    >
      <RowIcon item={item} size={32} />
      {nameOverride ?? (
        <>
          <span className="alk-files-grid__name">{displayNameOf(item)}</span>
          {adornment}
        </>
      )}
      <span className="alk-files-grid__meta">{formatSize(sizeOf(item))}</span>
    </div>
  );
}

/** Apply an action to the page's selection state against the rows on screen. Exported so
 *  the page and the grid can never disagree about what "visible order" means. */
export function applySelection(
  state: SelectionState,
  action: SelectionAction,
  rows: readonly Item[],
): SelectionState {
  return selectionReducer(
    state,
    action,
    rows.map((row) => row.id),
  );
}

export default Treegrid;

/**
 * The toolbar search field and the results it produces.
 *
 * A search opens on the whole drive and narrows to the folder on screen — the
 * two scopes sit in one dropdown beside the field, named for what they read:
 * the folder’s own name, and "Everywhere". Where there is no folder to narrow
 * to (a feed, the drive’s home) the dropdown is not drawn at all rather than
 * drawn dead. There is no Search button: it submitted a form with nothing to
 * submit, since the results are already on screen by the time a hand reaches
 * it.
 *
 * Cmd+F (Ctrl+F elsewhere) puts the caret in the field; Cmd+Shift+F does that
 * and widens the search to the whole drive.
 *
 * The state lives in the URL (`filterState`), so a search is linkable and
 * survives a reload; this component only reads and writes it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Select } from "@alkera/ui";

import type { Item } from "@/api/files";
import { enclosingPath, type SearchQueryResult } from "./useSearchQuery";
import { displayNameOf, kindLabel, LOCATION_COLUMN } from "@/lib/files/columns";
import type { FilterState, SearchScope } from "./filterState";
import { detectPlatform, type Platform } from "@/lib/platform";
import { emptySelection, selectionReducer, type SelectionAction } from "./state/selection";

/** The accelerator on this platform: Cmd on macOS, Ctrl everywhere else. */
export function usesCommandKey(event: KeyboardEvent, platform: Platform): boolean {
  return platform === "mac" ? event.metaKey : event.ctrlKey;
}

/** How much of the folder’s name the scope option shows before it gives way. */
const SCOPE_NAME_MAX = 20;

/** The box and the list it answers with are drawn in different parts of the
 *  page — the toolbar and the area below it — so the two ends of the walk find
 *  each other by id rather than through a prop threaded across the page. */
const SEARCH_FIELD_ID = "alk-files-search-input";
const SEARCH_RESULTS_ID = "alk-files-search-results";

/** The row at `index` in the results, or nothing where there is no such row. */
function resultRowAt(index: number): HTMLElement | null {
  const rows = document.querySelectorAll<HTMLElement>(`#${SEARCH_RESULTS_ID} [data-row-id]`);
  return rows[index] ?? null;
}

/**
 * The folder’s name, cut to something a toolbar control can hold.
 *
 * A chat names its working directory after its first message, so the name
 * reaching this control is routinely a sentence — long enough to push the field
 * off the toolbar if the control grew to fit it. The whole name stays on the
 * control’s `title`, so nothing that was cut is unreachable.
 */
export function elideScopeName(name: string, max: number = SCOPE_NAME_MAX): string {
  return name.length <= max ? name : `${name.slice(0, max - 1).trimEnd()}…`;
}

export interface SearchBarProps {
  state: FilterState;
  onChange: (next: FilterState) => void;
  /** The folder on screen, named on the scope that narrows to it. Absent where
   *  there is no folder — a feed, or the drive’s home — and the dropdown is then
   *  not drawn. */
  folderName?: string;
  platform?: Platform;
}

/**
 * The search field.
 *
 * The shortcut listener sits on the document because Cmd+F has to work while
 * focus is in the treegrid — which is where it always is when someone reaches
 * for it.
 */
export function SearchBar({ state, onChange, folderName, platform }: SearchBarProps) {
  const resolved = platform ?? detectPlatform();
  const inputRef = useRef<HTMLInputElement>(null);

  // Read off a ref so the shortcut listener is bound once rather than re-bound
  // on every keystroke in the field.
  const widen = useRef<() => void>(() => undefined);
  widen.current = () => {
    if (state.scope !== "drive") onChange({ ...state, scope: "drive" });
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() !== "f" || !usesCommandKey(event, resolved)) return;
      event.preventDefault();
      // Cmd+Shift+F widens to the whole drive on its way to the field: it is the
      // shortcut for "search further", not a second way to focus.
      if (event.shiftKey) widen.current();
      inputRef.current?.focus();
      inputRef.current?.select();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [resolved]);

  return (
    <form
      className="alk-files-search"
      role="search"
      onSubmit={(event) => {
        // Results stream in as the text is typed, so there is nothing for a submit to
        // do — and nothing to submit TO: Enter in the field must not reload the page.
        event.preventDefault();
      }}
    >
      {/* No visible label: the field carries its own name for assistive tech, and
          the scope dropdown beside it says what it reads. */}
      <input
        id={SEARCH_FIELD_ID}
        ref={inputRef}
        className="alk-files-search__input"
        type="search"
        autoComplete="off"
        spellCheck={false}
        value={state.text}
        aria-label="Search"
        placeholder="Search"
        aria-controls={SEARCH_RESULTS_ID}
        onChange={(event) => onChange({ ...state, text: event.target.value })}
        onKeyDown={(event) => {
          if (event.key === "Escape" && state.text !== "") {
            event.stopPropagation();
            onChange({ ...state, text: "" });
            return;
          }
          // Down walks into the hits. Every other key stays with the text: a
          // search box is a text field first, so Home, End and the sideways
          // arrows belong to what is typed in it.
          if (event.key !== "ArrowDown") return;
          const first = resultRowAt(0);
          if (first === null) return;
          event.preventDefault();
          first.focus();
        }}
      />
      {folderName === undefined ? null : (
        // The title rides the wrapper rather than the control: the trigger is the
        // Select's own button, and a name long enough to be cut is exactly the one
        // a reader needs in full.
        <div
          className="alk-files-search__scope"
          title={state.scope === "folder" ? folderName : undefined}
        >
          <Select
            aria-label="Search scope"
            size="sm"
            // With the check trailing, a folder name too long for the row is what
            // gives way — the tick keeps its box at the row's edge.
            tickSide="right"
            className="alk-files-search__scope-trigger"
            value={state.scope}
            onChange={(event) => onChange({ ...state, scope: event.target.value as SearchScope })}
          >
            <option value="folder">{elideScopeName(folderName)}</option>
            <option value="drive">Everywhere</option>
          </Select>
        </div>
      )}
    </form>
  );
}

export interface SearchResultsProps {
  result: SearchQueryResult;
  onOpen?: (item: Item) => void;
  /** The ids the page's menu, keyboard and details pane are aimed at. A search is
   *  the one surface whose rows live nowhere the reader can reach otherwise, so it
   *  reports its own selection the way a feed does. */
  onSelectionChange?: (ids: readonly string[]) => void;
  /** The rows behind those ids, every time the search answers again — a refetch is
   *  how a renamed row reaches the page. */
  onRowsChange?: (rows: readonly Item[]) => void;
  platform?: Platform;
}

/**
 * The results, in a treegrid with the path column visible.
 *
 * A search result is only useful with its location — two files called
 * `notes.md` are told apart by nothing else — so the path is a real column here
 * rather than a hover title, in IBM Plex Mono like every other path in the
 * product. It is the same column a feed draws, named and identified from the one
 * definition in `columns.ts`, at the depth a search needs: a search reaches across
 * the whole drive, so the cell reads the enclosing path rather than the one folder
 * name a feed row shows.
 */
export function SearchResults({
  result,
  onOpen,
  onSelectionChange,
  onRowsChange,
  platform,
}: SearchResultsProps) {
  const rows = result.items;
  const label = useMemo(() => `Search results for ${result.settledText}`, [result.settledText]);
  const here = platform ?? detectPlatform();

  // The same reducer the treegrid runs, over the rows the search is showing: a
  // click, a Shift range and a right-click outside the selection all mean here
  // exactly what they mean in a listing.
  const [selection, setSelection] = useState(emptySelection);
  const order = useMemo(() => rows.map((row) => row.id), [rows]);
  const dispatch = useCallback(
    (action: SelectionAction) => {
      setSelection((current) => selectionReducer(current, action, order));
    },
    [order],
  );

  // The one row Tab can land on. The selection is kept across answers, so once a
  // reader has walked onto a hit and then typed another character the focused id
  // routinely names a row the new answer does not list — and aiming the only tab
  // stop at it left the results reachable by nothing at all. The first hit takes
  // the stop back whenever the focused row is not among them.
  const tabbableId = useMemo(() => {
    const focused = selection.focusedId;
    if (focused !== null && order.includes(focused)) return focused;
    return rows[0]?.id ?? null;
  }, [order, rows, selection.focusedId]);

  // Reported after the commit and never from inside the updater, the way the feed
  // reports its own: a parent setState during the render phase is the "cannot
  // update a component while rendering a different component" error.
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

  const report = useRef(onRowsChange);
  useEffect(() => {
    report.current = onRowsChange;
  }, [onRowsChange]);
  // Keyed on what the rows ARE, not on the array they arrived in: the hook rebuilds
  // its filtered list on every render, so reporting on the array's identity handed
  // the page a new list forever and the two re-rendered each other without end.
  // The etag rides the key because a refetch is how a renamed row reaches the page.
  const rowsKey = useMemo(() => rows.map((row) => `${row.id}:${row.etag}`).join(" "), [rows]);
  const latest = useRef(rows);
  latest.current = rows;
  useEffect(() => {
    report.current?.(latest.current);
  }, [rowsKey]);

  if (!result.isActive) return null;

  if (rows.length === 0) {
    return (
      <div className="alk-files-search__results" role="status">
        {result.isFetching ? "Searching…" : `Nothing matches “${result.settledText}”.`}
      </div>
    );
  }

  return (
    <div
      id={SEARCH_RESULTS_ID}
      className="alk-files-search__results"
      role="treegrid"
      aria-label={label}
    >
      <div className="alk-files-search__row alk-files-search__row--head" role="row">
        <span role="columnheader">Name</span>
        <span role="columnheader">{LOCATION_COLUMN.label}</span>
        <span role="columnheader">Kind</span>
      </div>
      {rows.map((item, index) => (
        <div
          key={item.id}
          className="alk-files-search__row"
          role="row"
          aria-level={1}
          aria-posinset={index + 1}
          aria-setsize={rows.length}
          aria-selected={selection.selected.has(item.id)}
          data-row-id={item.id}
          tabIndex={tabbableId === item.id ? 0 : -1}
          onClick={(event) =>
            dispatch({
              type: "click",
              id: item.id,
              modifiers: {
                shift: event.shiftKey,
                accel: here === "mac" ? event.metaKey : event.ctrlKey,
              },
            })
          }
          onContextMenu={() => {
            // A right-click acts on the row under the pointer, and leaves a
            // selection it is already inside alone. The event still bubbles to
            // the page, which is what opens the menu.
            if (selection.selected.has(item.id)) return;
            dispatch({ type: "click", id: item.id, modifiers: { shift: false, accel: false } });
          }}
          onDoubleClick={() => onOpen?.(item)}
          onFocus={() => {
            // Arriving on a hit makes it the current one, so the details pane and
            // the row menu are aimed at what the reader is looking at.
            if (selection.focusedId === item.id) return;
            dispatch({ type: "click", id: item.id, modifiers: { shift: false, accel: false } });
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              onOpen?.(item);
              return;
            }
            if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
            event.preventDefault();
            if (event.key === "ArrowUp" && index === 0) {
              // Above the first hit is the box the query was typed in — the walk
              // is reversible, so a reader who overshoots is not stranded.
              document.getElementById(SEARCH_FIELD_ID)?.focus();
              return;
            }
            resultRowAt(event.key === "ArrowDown" ? index + 1 : index - 1)?.focus();
          }}
        >
          <span role="gridcell" className="alk-files-search__name">
            {displayNameOf(item)}
          </span>
          <span role="gridcell" className="alk-files-search__path" data-column={LOCATION_COLUMN.id}>
            {enclosingPath(item)}
          </span>
          <span role="gridcell">{kindLabel(item)}</span>
        </div>
      ))}
    </div>
  );
}

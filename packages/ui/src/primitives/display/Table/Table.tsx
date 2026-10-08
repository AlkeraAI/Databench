import {
  Children,
  cloneElement,
  Fragment,
  isValidElement,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type {
  HTMLAttributes,
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  ReactElement,
  ReactNode,
} from "react";

import { cx } from "../../cx";
import { useScrollShadow } from "../../../hooks";
import { TableFooter } from "./TablePager";

/** Per-column floor (px) for a flexible column. A responsive table stacks into cards once its
 *  container can't give every column at least this much — a content-independent threshold, so the fold
 *  point never depends on the data (which `table-layout: fixed` also guarantees for the un-stacked
 *  column widths). */
const MIN_COL_PX = 104;

/** Root font size assumed when resolving a `rem` colWidth to px. The app roots at 16px (its rem tokens
 *  assume it), so this stays a pure, DOM-free computation the fold point can rely on. */
const ROOT_PX = 16;

/** Resolve a `colWidths` entry to px for the stack threshold: a number is px; a `rem`/`px` string is
 *  converted; anything else (a flexible `undefined`, a `%`/`fr`/`em` unit) returns null so the caller
 *  treats it as one MIN_COL_PX floor. */
function resolveColPx(w: string | number | undefined): number | null {
  if (typeof w === "number") return w;
  if (typeof w === "string") {
    if (w.endsWith("rem")) return parseFloat(w) * ROOT_PX;
    if (w.endsWith("px")) return parseFloat(w);
  }
  return null;
}

/** The container width below which a responsive table stacks. Under `table-layout: fixed`, the fixed
 *  columns hold their exact widths and the flexible ones absorb whatever space is left (shrinking toward
 *  zero), so the table's real no-scroll minimum is the SUM OF THE FIXED column widths — not a flat
 *  `columns × MIN_COL_PX`, which underestimates a table carrying a few wide fixed columns and lets it
 *  scroll in the gap before it would stack (the crash register: ~700px of fixed columns yet a flat
 *  6 × 104 = 624px threshold, so it scrolled between 624px and 700px). The flat value is the floor for a
 *  table with no (or all-flexible) colWidths. Both terms are content-independent, so the fold point
 *  stays stable regardless of the data.
 *
 *  `stackAt` is an explicit px override for a page that wants its columns to fold SOONER (at a wider
 *  container) than the derived width — the derived value only knows the fixed track widths, not that a
 *  flexible column needs real breathing room to stay legible. When set, it's the floor; the table
 *  stacks at the LARGER of the override and the derived minimum, so an override can only make it stack
 *  earlier, never leave a scroll/crush gap the derived minimum already closes. */
export function stackThresholdPx(
  columnCount: number,
  colWidths: Array<string | number | undefined> | undefined,
  stackAt?: number,
): number {
  const flat = Math.max(1, columnCount) * MIN_COL_PX;
  let derived = flat;
  if (colWidths) {
    let sum = 0;
    for (let i = 0; i < columnCount; i++) {
      const px = resolveColPx(colWidths[i]);
      // A fixed column needs its exact width; a flexible one still needs a floor (MIN_COL_PX) to hold
      // its header + content legibly. Counting that floor is what folds the table BEFORE a flexible
      // column is squeezed too narrow for its header — so the header never has to clip hard, and (with
      // th/td clipping) columns can never overlap. Pages that want to fold even sooner pass `stackAt`.
      sum += px != null ? px : MIN_COL_PX;
    }
    derived = Math.max(flat, sum);
  }
  return stackAt != null ? Math.max(derived, stackAt) : derived;
}

/**
 * Stack a `responsive` table into record cards once its container gets too narrow to hold its columns —
 * `container width < thresholdPx` (see {@link stackThresholdPx}). It keys off the container's own width
 * (a ResizeObserver on the scroll box), NOT the viewport and NOT the cell content, so the fold point is
 * stable and a `table-layout: fixed` table — which fills its container and never overflows — still
 * stacks instead of crushing its columns down to nothing. Measuring the live width each tick means a
 * table that grows or shrinks (a split pane, a filter) folds and unfolds at the same threshold every
 * time, with no remembered state to go stale.
 */
function useStackThreshold(enabled: boolean, thresholdPx: number) {
  const blockRef = useRef<HTMLDivElement>(null);
  const [stacked, setStacked] = useState(false);

  useLayoutEffect(() => {
    if (!enabled) {
      setStacked(false);
      return;
    }
    const block = blockRef.current;
    const scroll = block?.querySelector<HTMLElement>(".alk-table-scroll");
    if (!block || !scroll) return;

    const threshold = thresholdPx;
    let raf = 0;
    const measure = () => {
      raf = 0;
      // A 0-width container is unmeasured or hidden (no layout yet) — don't stack until it has a real
      // width, so a freshly-mounted or offscreen table doesn't flash into cards.
      const width = scroll.clientWidth;
      setStacked(width > 0 && width < threshold);
    };
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(measure);
    };
    const observer = new ResizeObserver(schedule);
    observer.observe(scroll);
    schedule();
    return () => {
      observer.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
  }, [enabled, thresholdPx]);

  return { blockRef, stacked };
}

/** Flatten a column header node to its text, for a stacked cell's `data-label`. */
function nodeText(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement(node))
    return nodeText((node.props as { children?: ReactNode }).children);
  return "";
}

/** Card-mode role of a stacked cell, emitted as `data-cell` for table.css to place. A `field` cell
 *  (the default — no attribute) reads as a "Label  value" row in the card body; `primary` is the
 *  identity that titles the card; `actions` is the trailing action cluster that shares the card's top
 *  line with the primary (right-aligned) — so an un-headered actions cell never lands on its own
 *  full-width bottom row; `marker` is a label-less decorative cell (an unread dot) the card drops once
 *  it carries that state on its own edge; `wide` is a colSpan payload (an expanded detail) that runs
 *  the full width. */
type CellRole = "primary" | "actions" | "marker" | "wide";

/** The role of the cell at `index` in a row (colSpan cells are resolved to `wide` by the caller).
 *  Identity wins over actions if they somehow coincide; a label-less cell that is neither is a marker;
 *  everything else is a field. */
function cellRole(
  index: number,
  header: string,
  primaryIdx: number,
  actionsIdx: number,
): CellRole | undefined {
  if (index === primaryIdx) return "primary";
  if (index === actionsIdx) return "actions";
  if (header === "") return "marker";
  return undefined;
}

/** Elements inside a row that own their OWN activation — a click or Enter/Space on one of these must
 *  NOT also fire the row's `onRowClick` (a trailing "delete" button, an inline link, a select). A page
 *  can opt any other node out with `data-norowclick`. */
const ROW_INTERACTIVE =
  "a,button,input,select,textarea,label,[role='button'],[data-norowclick]";

/** Clone a data row `<tr>` so the WHOLE row activates `onRowClick(index)` — a pointer, Tab focus, and
 *  Enter/Space activation, plus the `data-rowclick` hook table.css styles (cursor + a keyboard focus
 *  ring). A click on an inner interactive element (see {@link ROW_INTERACTIVE}) is ignored so a trailing
 *  action never double-fires; keyboard activation fires only when the ROW itself holds focus (an inner
 *  control keeps its own Enter/Space). A Fragment row (a main row plus a colSpan detail row) makes only
 *  its FIRST `<tr>` clickable. The clone MERGES over the caller's props, so a per-row `aria-label` / key
 *  stays. This is why a page never re-rolls the onClick + tabIndex + keydown + `.rowLink` boilerplate. */
function withRowClick(
  row: ReactNode,
  index: number,
  onRowClick: (index: number) => void,
): ReactNode {
  if (isValidElement(row) && row.type === Fragment) {
    const frag = row as ReactElement<{ children?: ReactNode }>;
    let clicked = false;
    const inner = Children.toArray(frag.props.children).map((child) => {
      if (!clicked && isValidElement(child) && child.type === "tr") {
        clicked = true;
        return withRowClick(child, index, onRowClick);
      }
      return child;
    });
    return cloneElement(frag, undefined, inner);
  }
  if (!isValidElement(row) || row.type !== "tr") return row;
  const tr = row as ReactElement<{ tabIndex?: number }>;
  return cloneElement(tr as ReactElement<Record<string, unknown>>, {
    "data-rowclick": "",
    tabIndex: tr.props.tabIndex ?? 0,
    onClick: (e: ReactMouseEvent<HTMLTableRowElement>) => {
      if ((e.target as HTMLElement).closest(ROW_INTERACTIVE)) return;
      onRowClick(index);
    },
    onKeyDown: (e: ReactKeyboardEvent<HTMLTableRowElement>) => {
      // Only when the row itself holds focus — an inner control keeps its own Enter/Space.
      if (e.target !== e.currentTarget) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onRowClick(index);
      }
    },
  });
}

/** Clone a `<tr>`'s `<td>` cells for the responsive layout: every cell is tagged with its column's
 *  flattened header as `data-label` (so a stacked card can show "Label  value") and with its card-mode
 *  `data-cell` role (see {@link cellRole}), so the card places the identity, its actions, and the field
 *  block correctly instead of dumping the label-less actions cell onto a lone bottom row. A Fragment
 *  row (a main `<tr>` plus an expand-detail `<tr>`) is descended into so its inner rows are tagged too.
 *  `actionsIdx` defaults per row to the trailing cell when its column header is empty (the conventional
 *  un-headered actions cell); `stackActions` overrides it. Columns named in `stackWide` become
 *  label-less full-width card rows (the `wide` role a colSpan cell also gets). Non-`<tr>` children
 *  pass through untouched. */
function withCardCells(
  row: ReactNode,
  columns: ReactNode[],
  primaryIdx: number,
  stackActions: number | undefined,
  stackWide: number[] | undefined,
): ReactNode {
  if (isValidElement(row) && row.type === Fragment) {
    const frag = row as ReactElement<{ children?: ReactNode }>;
    const inner = Children.toArray(frag.props.children).map((child) =>
      withCardCells(child, columns, primaryIdx, stackActions, stackWide),
    );
    return cloneElement(frag, undefined, inner);
  }
  if (!isValidElement(row) || row.type !== "tr") return row;
  const tr = row as ReactElement<{ children?: ReactNode }>;
  const cells = Children.toArray(tr.props.children);
  const n = cells.length;
  // Default the actions cell to the trailing column when it has no header (the un-headered actions
  // convention) — so most tables need no per-page config; `stackActions` overrides it.
  const actionsIdx =
    stackActions ?? (n > 0 && nodeText(columns[n - 1]) === "" ? n - 1 : -1);
  const mapped = cells.map((cell, i) => {
    if (!isValidElement(cell)) return cell;
    const span = (cell.props as { colSpan?: number }).colSpan;
    const wide =
      (span != null && span > 1) || (stackWide?.includes(i) ?? false);
    const role: CellRole | undefined = wide
      ? "wide"
      : cellRole(i, nodeText(columns[i]), primaryIdx, actionsIdx);
    // A wide cell keeps an empty label (the card lets it run full-width, self-explanatory); every
    // other cell keeps its column label for the field ::before.
    const label = wide ? "" : nodeText(columns[i]);
    return cloneElement(cell as ReactElement<Record<string, unknown>>, {
      "data-label": label,
      ...(role ? { "data-cell": role } : {}),
    });
  });
  return cloneElement(tr, undefined, mapped);
}

// A data table that owns its chrome: header, horizontal scroll with edge shadows (shown only on the
// side that actually has more to scroll), a footer that states the row count, and an optional pager.
// The caller supplies ONLY the column header cells and the <tr> rows (as children) — cell rendering
// + selection stay theirs (see the `useRowSelection` hook for the selection boilerplate).
//
// By default the footer's left names WHICH rows are in view: a paginated table shows the range
// ("1–10 of 97"), an un-paged one shows its size ("6 rows"). Pass `footerStart` to override it (e.g.
// "3 of 6 selected"), or `null` to drop it.
//
// Two ways to paginate, and the footer's pager is the same either way — the caller never rolls its
// own prev/next:
//   • pageSize — CLIENT-side. All rows are children; Table slices them per page and owns the page.
//   • pagination — SERVER-side (controlled). The caller fetches one page and passes only those rows;
//     Table renders them as-is and drives the pager from `pagination` (page + pageSize + total),
//     computing the page count and the row range itself.
// The two are mutually exclusive. Styling props (className/style/data-*) forward to the root.
export interface TableServerPagination {
  /** 1-based current page — the page whose rows are passed as children. */
  page: number;
  /** Rows per page — sets the page count and the footer's row range. */
  pageSize: number;
  /** Total rows across every page — for the page count and the "x–y of total" range. */
  total: number;
  /** Requested 1-based page, already clamped to [1, pageCount]. Fetch it and update `page`. */
  onPageChange: (page: number) => void;
}

interface TableBaseProps extends Omit<
  HTMLAttributes<HTMLDivElement>,
  "children"
> {
  /** Header cells, left to right. A node per column (a label, or a custom cell like a select-all
   *  checkbox). A node that is itself a `<th>` element renders AS the header cell, so a caller can
   *  set cell attributes the content can't reach — `aria-sort` on a sorted column, a scope, an
   *  alignment style. */
  columns: ReactNode[];
  /** The left of the footer. Omit for the default (a row count, or a range when paged); pass a node to
   *  override it (e.g. a selection count); pass `null` to show no left content at all. */
  footerStart?: ReactNode;
  /** When the table's own width gets narrow, stack each row into a compact record card instead of a
   *  horizontal scroll — the identity + actions on the card's top line, the rest as a label/value
   *  block below. Each cell's column label is injected from `columns` as a `data-label`. */
  responsive?: boolean;
  /** Card-mode (stacked) identity column — the cell that titles the card, sharing the top line with
   *  the actions. Defaults to the first column. */
  stackPrimary?: number;
  /** Card-mode (stacked) actions column — the cell that becomes the card's top-right action cluster.
   *  Defaults to the trailing column when its header is empty (the conventional un-headered actions
   *  cell); pass an index to override, or when the actions aren't the last column. */
  stackActions?: number;
  /** Card-mode (stacked) columns whose value is self-explanatory without its column label -- a status
   *  chip, a chip cluster. Each reads as a label-less full-width card row instead of the default
   *  "Label  value" field pair. */
  stackWide?: number[];
  /** Vertical alignment of the data cells. The default `middle` centres single-line neighbours; pass
   *  `top` when a table's rows carry multi-line cells (a title over a detail), so a short cell (a
   *  status chip, an action) lines up with the tall cell's first line instead of floating mid-row. */
  verticalAlign?: "middle" | "top";
  /** Force the table to stack into cards at a WIDER container than its columns alone would trigger —
   *  a px floor on the fold point (see {@link stackThresholdPx}). Use it when a flexible column needs
   *  more room than `table-layout: fixed` will grant it before the text gets too cramped to read. It
   *  only ever makes the table stack SOONER: the effective threshold is `max(derived, stackAt)`. */
  stackAt?: number;
  /** Per-column widths, applied via a `<colgroup>` under `table-layout: fixed` — so a narrow column
   *  (a date, a status, an actions cluster) stays narrow and a wide one stays wide, all independent of
   *  the cell content (no shift as the data changes). One entry per column, left to right; `undefined`
   *  lets that column share the leftover space equally (a flexible content column). A number is px; a
   *  string is any CSS width. Omit the prop entirely for equal columns. */
  colWidths?: Array<string | number | undefined>;
  /** Make each data row clickable — the whole row opens its detail (a drawer, a navigation). The
   *  handler gets the row's index in the ORIGINAL children (stable across pagination — client-paged
   *  rows report their absolute index, not their in-page position), so a page maps it back to its own
   *  data array. The row gains a pointer, a focus ring, Tab focus, and Enter/Space activation, so a page
   *  never re-rolls that boilerplate or a `.rowLink` class. A click or key on an inner control (a button,
   *  link, field, or a node marked `data-norowclick`) is ignored, so trailing actions don't double-fire. */
  onRowClick?: (index: number) => void;
  /** The <tr> data rows. */
  children: ReactNode;
}

/** Client-side pagination — rows per page (omit or 0 for a single, un-paged table). */
interface ClientPagedProps {
  pageSize?: number;
  pagination?: never;
}

/** Server-side (controlled) pagination — the caller owns the page and fetches its rows. */
interface ServerPagedProps {
  pageSize?: never;
  pagination: TableServerPagination;
}

// `pageSize` (client) and `pagination` (server) are mutually exclusive BY CONSTRUCTION — passing both
// is a compile error, so the invariant can't be violated the way a comment-only rule could.
export type TableProps = TableBaseProps & (ClientPagedProps | ServerPagedProps);

/** Footer-left content when the table is NOT paginated — its total row count ("6 rows" / "1 row"). */
export function rowCountLabel(count: number): string {
  return `${count} ${count === 1 ? "row" : "rows"}`;
}

/** Footer-left content for a paginated table — the current page's row range within the total
 *  ("1–10 of 97"). When `total` is 0 the range is meaningless, so `start`/`end` are ignored and it
 *  returns the plain "0 rows" count. Exported so callers/tests build the expected label from this one
 *  source instead of copying the format. */
export function rowRangeLabel(
  start: number,
  end: number,
  total: number,
): string {
  return total === 0 ? rowCountLabel(0) : `${start}–${end} of ${total}`;
}

export function Table({
  columns,
  pageSize,
  pagination,
  footerStart,
  responsive,
  stackPrimary,
  stackActions,
  stackWide,
  stackAt,
  colWidths,
  verticalAlign,
  onRowClick,
  className,
  children,
  ...rest
}: TableProps) {
  const rows = Children.toArray(children);
  const serverPaged = pagination != null;
  const size = serverPaged ? pagination.pageSize : (pageSize ?? 0);
  const clientPaged = !serverPaged && size > 0;
  const paged = serverPaged || clientPaged;
  // The true total: the caller's `total` in server mode, else every rendered row in client mode.
  const total = serverPaged ? pagination.total : rows.length;

  const [page, setPage] = useState(1);

  // Resolve the active page + count. Server mode trusts the caller's page (its children ARE that
  // page); client mode slices the children and owns the page state. Clamp on render so a shrinking
  // row set (e.g. a filter) can't strand the view past the last page.
  const pageCount = paged
    ? Math.max(1, Math.ceil(total / Math.max(1, size)))
    : 1;
  const current = Math.min(
    Math.max(1, serverPaged ? pagination.page : page),
    pageCount,
  );
  const go = serverPaged
    ? (p: number) =>
        pagination.onPageChange(Math.min(Math.max(1, p), pageCount))
    : (p: number) => setPage(Math.min(Math.max(1, p), pageCount));

  const visible = clientPaged
    ? rows.slice((current - 1) * size, current * size)
    : rows;

  // Footer-left default: a paged table names WHICH rows are in view ("1–10 of 97"), an un-paged one
  // just its size ("6 rows"). Omitted (undefined) → this default; an explicit node overrides it;
  // explicit null shows none.
  const rangeStart = total === 0 ? 0 : (current - 1) * size + 1;
  const rangeEnd = Math.min(current * size, total);
  const autoFootStart = paged
    ? rowRangeLabel(rangeStart, rangeEnd, total)
    : rowCountLabel(total);
  const footStart = footerStart === undefined ? autoFootStart : footerStart;

  // Responsive mode tags each cell with its column label + card-mode role, so a stacked row reads as a
  // record card (identity + actions on top, a label/value block below) instead of a scroll. onRowClick
  // makes the whole row activate (see withRowClick) with its ABSOLUTE index — client-paged rows report
  // their position in the full child set, not within the current page — so a page maps back to its data.
  const primaryIdx = stackPrimary ?? 0;
  const body = visible.map((row, i) => {
    const index = clientPaged ? (current - 1) * size + i : i;
    const clickable = onRowClick ? withRowClick(row, index, onRowClick) : row;
    return responsive
      ? withCardCells(clickable, columns, primaryIdx, stackActions, stackWide)
      : clickable;
  });
  const scrollRef = useScrollShadow<HTMLDivElement>();
  const { blockRef, stacked } = useStackThreshold(
    Boolean(responsive),
    stackThresholdPx(columns.length, colWidths, stackAt),
  );
  return (
    <div
      ref={blockRef}
      className={cx("alk-table-block", className)}
      {...rest}
      data-stacked={stacked ? "" : undefined}
      data-valign={verticalAlign === "top" ? "top" : undefined}
    >
      <div className="alk-table-scroll" ref={scrollRef}>
        {/* table-layout:fixed keeps column widths independent of cell content — they never shift when
            the data changes. The table fills its container (never overflows); when the container drops
            below columns × MIN_COL_PX the block stacks (useStackThreshold) instead of crushing them. */}
        <table className="alk-table">
          {colWidths ? (
            <colgroup>
              {columns.map((_, i) => {
                const w = colWidths[i];
                return (
                  <col
                    key={i}
                    style={
                      w != null
                        ? { width: typeof w === "number" ? `${w}px` : w }
                        : undefined
                    }
                  />
                );
              })}
            </colgroup>
          ) : null}
          <thead>
            <tr>
              {columns.map((c, i) =>
                // A column that is itself a <th> is the header cell — render it as-is (never nested
                // in another th), so a caller can carry aria-sort / scope / alignment on the cell.
                isValidElement(c) && c.type === "th" ? (
                  <Fragment key={i}>{c}</Fragment>
                ) : (
                  // A narrow column clips its own heading ("Used this wind…") with nothing to hover,
                  // so the full wording rides the cell. A caller passing its own <th> owns this.
                  <th key={i} title={nodeText(c) || undefined}>
                    {c}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
      </div>
      <TableFooter
        start={footStart}
        pager={paged ? { page: current, pageCount, onPage: go } : undefined}
      />
    </div>
  );
}

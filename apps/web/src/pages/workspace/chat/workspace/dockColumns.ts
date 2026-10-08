/**
 * Which of the listing's columns survive the width the reader has dragged the
 * dock to.
 *
 * The Files page answers a narrow VIEWPORT by folding the row (`data-narrow`),
 * which is the right answer for a phone and the wrong one here: the dock is a
 * narrow column inside a wide window, so that breakpoint never fires and the
 * five-column table keeps its 596px of floors inside a 395px pane — headers cut
 * to "K…", "Mo…", "Owne", row values wrapped and shaved. The listing is the
 * page's, so this decides the same thing the page's media query decides, from
 * the pane's own width instead of the window's.
 *
 * Name is never given up and is allowed to shrink below its page floor: a row
 * whose name is cut with an ellipsis is still a row a reader can use, and a row
 * pushed off the side is not. The rest are given up in the order they stop
 * earning their width, and a column added to the page joins by adding a row to
 * the table below rather than by editing the arithmetic.
 */

import { FILES_COLUMNS } from "@/lib/files/columns";

export type DockColumnId = (typeof FILES_COLUMNS)[number]["id"];

/** What a column costs and what track it draws, taken from the page's own list
 *  layout (`files-page.css`) so the dock is the same table, narrowed. Name's
 *  floor is what it needs to stay READABLE, not to stay on screen — it is the
 *  budget the others are measured against, never a track that can overflow. */
const DOCK_COLUMNS: Readonly<Record<DockColumnId, { floor: number; track: string }>> = {
  name: { floor: 200, track: "minmax(0, 2.6fr)" },
  kind: { floor: 72, track: "minmax(72px, 0.9fr)" },
  size: { floor: 64, track: "minmax(64px, 0.7fr)" },
  modified: { floor: 150, track: "minmax(150px, 1.6fr)" },
  owner: { floor: 110, track: "minmax(110px, 1.1fr)" },
};

/**
 * The secondary columns, most worth keeping first.
 *
 * Size comes first because it is the cheapest fact on the row and the one that
 * says an agent's file is not empty; Modified next, because a folder a machine
 * is writing is read by when it last changed. Kind and Owner go first, as they
 * do on the page's own narrow row — the icon beside the name already says what
 * a row is, and every file in a chat's own folder has the same owner.
 */
export const DOCK_SECONDARY_BY_PRIORITY: readonly DockColumnId[] = [
  "size",
  "modified",
  "kind",
  "owner",
];

export interface DockColumnLayout {
  /** In the listing's own column order, so the tracks line up with the cells. */
  readonly visible: readonly DockColumnId[];
  /** The ones the dock gave up, for the rules that hide their cells. */
  readonly hidden: readonly DockColumnId[];
  /** `grid-template-columns` for exactly the visible cells. */
  readonly template: string;
}

/**
 * The columns a dock this wide can draw whole.
 *
 * A width of zero is a pane the browser has not laid out yet, not a pane with
 * no room: it keeps every column, so the first paint is the full table and only
 * a real measurement takes one away.
 */
export function dockColumnLayout(width: number): DockColumnLayout {
  const kept = new Set<DockColumnId>(["name"]);
  if (width <= 0) {
    for (const id of DOCK_SECONDARY_BY_PRIORITY) kept.add(id);
  } else {
    let left = width - DOCK_COLUMNS.name.floor;
    for (const id of DOCK_SECONDARY_BY_PRIORITY) {
      const floor = DOCK_COLUMNS[id].floor;
      // One order, honoured strictly: a cheaper column slipping in past one the
      // pane could not afford would make the table's contents depend on the
      // exact pixel the reader stopped dragging at.
      if (left < floor) break;
      left -= floor;
      kept.add(id);
    }
  }

  const order = FILES_COLUMNS.map((column) => column.id);
  const visible = order.filter((id) => kept.has(id));
  return {
    visible,
    hidden: order.filter((id) => !kept.has(id)),
    template: visible.map((id) => DOCK_COLUMNS[id].track).join(" "),
  };
}

/** The hidden set as the CSS reads it: a space-separated list matched with
 *  `[data-hide-columns~="owner"]`. Absent when the dock gave up nothing, so the
 *  attribute itself says whether the table is whole. */
export function hiddenColumnsAttr(layout: DockColumnLayout): string | undefined {
  return layout.hidden.length > 0 ? layout.hidden.join(" ") : undefined;
}

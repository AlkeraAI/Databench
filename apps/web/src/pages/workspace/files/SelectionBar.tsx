/**
 * The bar under the listing: what is selected, what it weighs, and the actions
 * that act on all of it.
 *
 * A multi-selection had nowhere to report itself. The details pane speaks for
 * one row, so the count of what a bulk trash or a bulk move was about to touch
 * lived only in the reader's head — and a Shift-range that caught one row more
 * than intended looked exactly like one that did not.
 *
 * It offers no action of its own. The buttons ARE the context menu's rows,
 * filtered to the ones that act on a whole selection and enabled, so the bar
 * cannot offer something the menu refuses or run it a different way. A row the
 * menu disables is left out here rather than drawn dead: the bar is what can be
 * done, and the menu is where a refusal is explained.
 */

import type { ContextMenuItem } from "@alkera/ui";

import type { Item } from "@/api/files";
import { formatBytes } from "@/lib/format/bytes";
import { FILES_SOFT_THRESHOLD_ROWS } from "@/lib/limits";
import { sizeOf } from "@/lib/files/columns";
import type { MenuActionId } from "./contextMenuItems";

/** The actions that mean something for more than one row, in the order the menu
 *  lists them. A new bulk action is a row here and nothing else. */
export const BULK_ACTIONS: readonly MenuActionId[] = [
  "download",
  "copy",
  "cut",
  "move-to",
  "copy-to",
  "duplicate",
  "trash",
];

export interface SelectionSummary {
  count: number;
  /** The total of the rows that have a size, and how many of them did. A folder
   *  whose stats have not landed has none, and must not be counted as zero. */
  bytes: number | null;
  measured: number;
}

/** How much of the folder the listing is holding, so a selection of everything
 *  on screen can say whether that IS everything. */
export interface SelectionScope {
  /** Rows the listing has paged in so far. */
  readonly loaded: number;
  /** The folder's direct children, when it is known. */
  readonly total: number | null;
  /** More rows exist beyond the ones loaded. */
  readonly hasMore: boolean;
}

/** One locale for every count in the bar: the sentence and the button must not
 *  disagree about how a thousand is written. */
const NUMBER = new Intl.NumberFormat("en-US");

export function summarize(selection: readonly Item[]): SelectionSummary {
  let bytes = 0;
  let measured = 0;
  for (const row of selection) {
    const size = sizeOf(row);
    if (size === null) continue;
    bytes += size;
    measured += 1;
  }
  return { count: selection.length, bytes: measured === 0 ? null : bytes, measured };
}

/** True when the selection is every row the listing holds and the folder holds
 *  more — the one case where a bare count reads as the whole folder and is not. */
function coversOnlyWhatIsLoaded(count: number, scope: SelectionScope | undefined): boolean {
  return scope !== undefined && scope.hasMore && count > 0 && count >= scope.loaded;
}

/** "3 items · 12.4 MB" — and "at least" when a row in the selection has no size
 *  yet, because the total is then a floor rather than the answer.
 *
 *  Select-all reaches only the rows that have been paged in, so a selection of
 *  the whole loaded listing in a folder that holds more says how much of the
 *  folder it is. Without that, a thousand rows out of three thousand read as
 *  "1000 items" — an accurate number that means the wrong thing next to Move to
 *  trash. The size is left off that sentence: the count is what a destructive
 *  action is judged on, and two numbers in one line is where a reader stops
 *  reading. */
export function describeSelection(
  summary: SelectionSummary,
  scope?: SelectionScope,
): string {
  const items = `${NUMBER.format(summary.count)} ${summary.count === 1 ? "item" : "items"}`;
  if (coversOnlyWhatIsLoaded(summary.count, scope)) {
    const shown = NUMBER.format(summary.count);
    return scope?.total != null && scope.total > summary.count
      ? `${shown} of ${NUMBER.format(scope.total)} items selected`
      : `${shown} items selected (the folder holds more)`;
  }
  const size = summary.bytes === null ? null : formatBytes(summary.bytes);
  if (size === null) return items;
  if (summary.measured === summary.count) return `${items} · ${size}`;
  return `${items} · at least ${size}`;
}

export interface SelectionBarProps {
  selection: readonly Item[];
  /** The menu the page built for this same selection. */
  menuItems: readonly ContextMenuItem[];
  /** How much of the folder is on screen. Omitted by a surface with no folder
   *  behind it — a feed, a search, the trash — where there is no whole for the
   *  selection to be part of. */
  scope?: SelectionScope;
  /** Page the rest of the folder in and select it too. Offered only while the
   *  selection is everything loaded and the folder holds more. */
  onSelectRest?: () => void;
}

export function SelectionBar({ selection, menuItems, scope, onSelectRest }: SelectionBarProps) {
  // One row says everything it has to say in the details pane; a bar for it
  // would be a second copy of the same facts under the same listing.
  if (selection.length < 2) return null;

  const byId = new Map(menuItems.map((row) => [row.id as MenuActionId, row]));
  const actions = BULK_ACTIONS.map((id) => byId.get(id)).filter(
    (row): row is ContextMenuItem => row !== undefined && row.disabled === undefined,
  );
  const partial = coversOnlyWhatIsLoaded(selection.length, scope);
  // Taking the rest of the folder pages every remaining row in and holds every
  // id in memory, so the offer stops where the listing's own ceiling does: a
  // folder past it is one the button cannot honestly promise. The count above
  // still says how much of the folder is selected either way.
  const reachable =
    scope?.total != null && scope.total > selection.length
      ? scope.total <= FILES_SOFT_THRESHOLD_ROWS
      : true;
  const rest =
    partial && reachable && onSelectRest
      ? scope?.total != null && scope.total > selection.length
        ? `Select all ${NUMBER.format(scope.total)}`
        : "Select all"
      : null;

  return (
    <div className="alk-files-selection" role="group" aria-label="Selection">
      {/* What the selection now is, said as it changes: the bar appears and its
          count moves under a keyboard that never leaves the rows, so a reader
          who cannot see it would otherwise act on a selection they were never
          told about. */}
      <span className="alk-files-selection__count" role="status">
        {describeSelection(summarize(selection), scope)}
      </span>
      {rest && (
        <button
          type="button"
          className="alk-files-selection__action"
          onClick={() => onSelectRest?.()}
        >
          {rest}
        </button>
      )}
      <span className="alk-files-selection__actions">
        {actions.map((action) => (
          <button
            key={action.id}
            type="button"
            className="alk-files-selection__action"
            onClick={() => action.onSelect?.()}
          >
            {action.label}
          </button>
        ))}
      </span>
    </div>
  );
}

export default SelectionBar;

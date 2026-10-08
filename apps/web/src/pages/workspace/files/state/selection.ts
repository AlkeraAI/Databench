/**
 * Selection state for the Files treegrid.
 *
 * Pure: every action takes the currently visible row order, so a range, a
 * select-all or a focus move can never reach a row the user cannot see.
 */

export interface SelectionState {
  /** Ids of the selected rows. */
  readonly selected: ReadonlySet<string>;
  /** Endpoint a Shift range grows from; set by every non-extending click. */
  readonly anchorId: string | null;
  /** Row the roving tabindex sits on. */
  readonly focusedId: string | null;
}

/** Modifier state of a pointer click, already normalised for the platform. */
export interface ClickModifiers {
  /** Shift held — extend from the anchor. */
  readonly shift: boolean;
  /** Cmd on macOS, Ctrl elsewhere — toggle without collapsing the selection. */
  readonly accel: boolean;
}

export type FocusKey = 'ArrowDown' | 'ArrowUp' | 'Home' | 'End';

export type SelectionAction =
  | { readonly type: 'click'; readonly id: string; readonly modifiers: ClickModifiers }
  | { readonly type: 'select-all' }
  | { readonly type: 'clear' }
  | {
      readonly type: 'drag-select';
      readonly fromIndex: number;
      readonly toIndex: number;
      readonly additive: boolean;
    }
  | {
      readonly type: 'focus-move';
      readonly key: FocusKey;
      /** Shift held — carry the selection along with the focus. */
      readonly extend: boolean;
      /** Accel held — move the focus and leave the selection alone. */
      readonly preserve: boolean;
    };

export const emptySelection: SelectionState = {
  selected: new Set<string>(),
  anchorId: null,
  focusedId: null,
};

function rangeIds(order: readonly string[], a: string, b: string): string[] | null {
  const start = order.indexOf(a);
  const end = order.indexOf(b);
  if (start === -1 || end === -1) return null;
  return order.slice(Math.min(start, end), Math.max(start, end) + 1);
}

function sliceByIndex(order: readonly string[], from: number, to: number): string[] {
  if (order.length === 0) return [];
  const lo = Math.max(0, Math.min(from, to));
  const hi = Math.min(order.length - 1, Math.max(from, to));
  if (hi < lo) return [];
  return order.slice(lo, hi + 1);
}

function nextFocusIndex(order: readonly string[], current: number, key: FocusKey): number {
  switch (key) {
    case 'Home':
      return 0;
    case 'End':
      return order.length - 1;
    case 'ArrowDown':
      return current === -1 ? 0 : Math.min(order.length - 1, current + 1);
    case 'ArrowUp':
      return current === -1 ? 0 : Math.max(0, current - 1);
  }
}

export function selectionReducer(
  state: SelectionState,
  action: SelectionAction,
  visibleOrder: readonly string[],
): SelectionState {
  switch (action.type) {
    case 'click': {
      const { id, modifiers } = action;
      if (modifiers.shift) {
        // A range needs somewhere to grow from; with no anchor it is one row.
        const range = state.anchorId === null ? null : rangeIds(visibleOrder, state.anchorId, id);
        if (range === null) {
          return { selected: new Set([id]), anchorId: id, focusedId: id };
        }
        const selected = modifiers.accel ? new Set(state.selected) : new Set<string>();
        for (const rowId of range) selected.add(rowId);
        // The anchor survives so growing and shrinking the range is reversible.
        return { selected, anchorId: state.anchorId, focusedId: id };
      }
      if (modifiers.accel) {
        const selected = new Set(state.selected);
        if (selected.has(id)) selected.delete(id);
        else selected.add(id);
        return { selected, anchorId: id, focusedId: id };
      }
      return { selected: new Set([id]), anchorId: id, focusedId: id };
    }
    case 'select-all':
      return {
        selected: new Set(visibleOrder),
        anchorId: visibleOrder[0] ?? null,
        focusedId: state.focusedId ?? visibleOrder[0] ?? null,
      };
    case 'clear':
      // Escape drops the selection but keeps the roving tabindex somewhere real.
      return { selected: new Set<string>(), anchorId: null, focusedId: state.focusedId };
    case 'drag-select': {
      const rows = sliceByIndex(visibleOrder, action.fromIndex, action.toIndex);
      const selected = action.additive ? new Set(state.selected) : new Set<string>();
      for (const rowId of rows) selected.add(rowId);
      return {
        selected,
        anchorId: visibleOrder[action.fromIndex] ?? state.anchorId,
        focusedId: rows.length === 0 ? state.focusedId : (rows[rows.length - 1] ?? state.focusedId),
      };
    }
    case 'focus-move': {
      if (visibleOrder.length === 0) return state;
      const current = state.focusedId === null ? -1 : visibleOrder.indexOf(state.focusedId);
      const index = nextFocusIndex(visibleOrder, current, action.key);
      const id = visibleOrder[index];
      if (id === undefined) return state;
      if (action.preserve) {
        return { ...state, focusedId: id };
      }
      if (action.extend) {
        const anchor = state.anchorId ?? state.focusedId ?? id;
        const range = rangeIds(visibleOrder, anchor, id) ?? [id];
        return { selected: new Set(range), anchorId: anchor, focusedId: id };
      }
      return { selected: new Set([id]), anchorId: id, focusedId: id };
    }
  }
}

/**
 * The selection after rows have left the listing — a delete, a move out of the
 * folder, a filter that no longer matches.
 *
 * The focus moves to the row that took the deleted one's place, or to the one
 * before it when the last row went. Without this a delete left `focusedId`
 * naming a row nobody can see: the roving tab stop had nowhere to sit, so the
 * browser dropped focus out of the grid to the document, and the next arrow key
 * started over from whichever row was last clicked.
 *
 * It fires only where the listing is recognisably the same one minus some rows.
 * Walking into another folder replaces every id, and picking a "successor" there
 * would select a row in a folder the person has only just arrived in.
 */
export function reconcileSelection(
  state: SelectionState,
  before: readonly string[],
  after: readonly string[],
): SelectionState {
  const live = new Set(after);
  const survivors = before.some((id) => live.has(id));
  const selected = new Set([...state.selected].filter((id) => live.has(id)));
  const focusHeld = state.focusedId !== null && live.has(state.focusedId);
  if (focusHeld && selected.size === state.selected.size) return state;

  if (!focusHeld && state.focusedId !== null && before.includes(state.focusedId) && survivors) {
    const gone = before.indexOf(state.focusedId);
    const successor =
      before.slice(gone + 1).find((id) => live.has(id)) ??
      [...before.slice(0, gone)].reverse().find((id) => live.has(id)) ??
      null;
    if (successor !== null) {
      // The neighbour is what the next keystroke acts on, so it is selected on
      // its own: leaving the old anchor behind is what made the selection snap
      // back to a row the person had left several presses ago.
      return { selected: new Set([successor]), anchorId: successor, focusedId: successor };
    }
  }

  const anchorId = state.anchorId !== null && live.has(state.anchorId) ? state.anchorId : null;
  return { selected, anchorId, focusedId: focusHeld ? state.focusedId : null };
}

/** Live-region text for the selection count. */
export function describeSelection(state: SelectionState): string {
  const count = state.selected.size;
  if (count === 0) return 'No items selected';
  return count === 1 ? '1 item selected' : `${count} items selected`;
}

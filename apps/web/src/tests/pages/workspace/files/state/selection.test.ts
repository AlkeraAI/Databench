import { describe, expect, it } from 'vitest';

import {
  type ClickModifiers,
  type SelectionAction,
  type SelectionState,
  describeSelection,
  emptySelection,
  reconcileSelection,
  selectionReducer,
} from '@/pages/workspace/files/state/selection';

const ORDER = ['a', 'b', 'c', 'd', 'e'] as const;

const PLAIN: ClickModifiers = { shift: false, accel: false };
const ACCEL: ClickModifiers = { shift: false, accel: true };
const SHIFT: ClickModifiers = { shift: true, accel: false };
const SHIFT_ACCEL: ClickModifiers = { shift: true, accel: true };

function run(
  actions: readonly SelectionAction[],
  order: readonly string[] = ORDER,
  initial: SelectionState = emptySelection,
): SelectionState {
  return actions.reduce((state, action) => selectionReducer(state, action, order), initial);
}

function click(id: string, modifiers: ClickModifiers): SelectionAction {
  return { type: 'click', id, modifiers };
}

function ids(state: SelectionState): string[] {
  return [...state.selected].sort();
}

describe('selectionReducer — click modifiers', () => {
  it('a plain click replaces the selection and sets the anchor', () => {
    const state = run([click('b', PLAIN), click('d', PLAIN)]);
    expect(ids(state)).toEqual(['d']);
    expect(state.anchorId).toBe('d');
    expect(state.focusedId).toBe('d');
  });

  it('a plain click on an already-selected row keeps it selected (the negative twin of accel-click)', () => {
    const state = run([click('b', ACCEL), click('c', ACCEL), click('b', PLAIN)]);
    expect(ids(state)).toEqual(['b']);
  });

  it('accel-click adds an unselected row', () => {
    const state = run([click('b', PLAIN), click('d', ACCEL)]);
    expect(ids(state)).toEqual(['b', 'd']);
  });

  it('accel-click on a selected row deselects it', () => {
    const state = run([click('b', PLAIN), click('d', ACCEL), click('b', ACCEL)]);
    expect(ids(state)).toEqual(['d']);
  });

  it('accel-click that deselects still moves the anchor and the focus to that row', () => {
    const state = run([click('b', PLAIN), click('b', ACCEL)]);
    expect(ids(state)).toEqual([]);
    expect(state.anchorId).toBe('b');
    expect(state.focusedId).toBe('b');
  });

  it('shift-click with no anchor selects exactly one row', () => {
    const state = run([click('c', SHIFT)]);
    expect(ids(state)).toEqual(['c']);
    expect(state.anchorId).toBe('c');
  });

  it('shift-click extends from the anchor and replaces the rest of the selection', () => {
    const state = run([click('b', PLAIN), click('d', SHIFT)]);
    expect(ids(state)).toEqual(['b', 'c', 'd']);
    expect(state.anchorId).toBe('b');
  });

  it('shift-click backwards over the anchor selects the same range', () => {
    const state = run([click('d', PLAIN), click('b', SHIFT)]);
    expect(ids(state)).toEqual(['b', 'c', 'd']);
  });

  it('a second shift-click re-grows the range from the same anchor instead of stacking', () => {
    const state = run([click('b', PLAIN), click('e', SHIFT), click('c', SHIFT)]);
    expect(ids(state)).toEqual(['b', 'c']);
    expect(state.anchorId).toBe('b');
  });

  it('shift-click after an accel-click uses the last accel-clicked row as the anchor', () => {
    const state = run([click('a', PLAIN), click('c', ACCEL), click('e', SHIFT)]);
    expect(ids(state)).toEqual(['c', 'd', 'e']);
    expect(state.anchorId).toBe('c');
  });

  it('shift+accel-click unions the range onto the existing selection', () => {
    const state = run([click('a', PLAIN), click('c', ACCEL), click('d', SHIFT_ACCEL)]);
    expect(ids(state)).toEqual(['a', 'c', 'd']);
  });

  it('shift-click without accel discards the earlier selection the union would have kept', () => {
    const state = run([click('a', PLAIN), click('c', ACCEL), click('d', SHIFT)]);
    expect(ids(state)).toEqual(['c', 'd']);
  });

  it('shift-click falls back to a single row when the anchor left the visible order', () => {
    const anchored = run([click('b', PLAIN)]);
    const state = selectionReducer(anchored, click('d', SHIFT), ['c', 'd', 'e']);
    expect(ids(state)).toEqual(['d']);
    expect(state.anchorId).toBe('d');
  });
});

describe('selectionReducer — select all and clear', () => {
  it('select-all takes the visible order and nothing else', () => {
    const state = selectionReducer(emptySelection, { type: 'select-all' }, ['b', 'c']);
    expect(ids(state)).toEqual(['b', 'c']);
  });

  it('select-all drops a previously selected row that is no longer visible', () => {
    const filtered = ['c', 'd'];
    const state = run([click('a', PLAIN), { type: 'select-all' }], filtered);
    expect(ids(state)).toEqual(['c', 'd']);
    expect(state.selected.has('a')).toBe(false);
  });

  it('select-all over an empty folder selects nothing and clears the anchor', () => {
    const state = selectionReducer(emptySelection, { type: 'select-all' }, []);
    expect(ids(state)).toEqual([]);
    expect(state.anchorId).toBeNull();
  });

  it('clear empties the selection and the anchor but keeps the focused row', () => {
    const state = run([click('c', PLAIN), { type: 'clear' }]);
    expect(ids(state)).toEqual([]);
    expect(state.anchorId).toBeNull();
    expect(state.focusedId).toBe('c');
  });

  it('a shift-click after clear has no anchor left and selects one row', () => {
    const state = run([click('a', PLAIN), { type: 'clear' }, click('d', SHIFT)]);
    expect(ids(state)).toEqual(['d']);
  });
});

describe('selectionReducer — drag select', () => {
  it('a rectangle over row indices selects the rows it covers', () => {
    const state = run([{ type: 'drag-select', fromIndex: 1, toIndex: 3, additive: false }]);
    expect(ids(state)).toEqual(['b', 'c', 'd']);
    expect(state.anchorId).toBe('b');
  });

  it('a rectangle dragged upwards covers the same rows', () => {
    const state = run([{ type: 'drag-select', fromIndex: 3, toIndex: 1, additive: false }]);
    expect(ids(state)).toEqual(['b', 'c', 'd']);
  });

  it('a non-additive drag replaces the earlier selection', () => {
    const state = run([
      click('e', PLAIN),
      { type: 'drag-select', fromIndex: 0, toIndex: 1, additive: false },
    ]);
    expect(ids(state)).toEqual(['a', 'b']);
  });

  it('an additive drag keeps the earlier selection', () => {
    const state = run([
      click('e', PLAIN),
      { type: 'drag-select', fromIndex: 0, toIndex: 1, additive: true },
    ]);
    expect(ids(state)).toEqual(['a', 'b', 'e']);
  });

  it('a rectangle past the last row is clamped to the rows that exist', () => {
    const state = run([{ type: 'drag-select', fromIndex: 3, toIndex: 99, additive: false }]);
    expect(ids(state)).toEqual(['d', 'e']);
  });

  it('a rectangle over an empty folder selects nothing', () => {
    const state = selectionReducer(
      emptySelection,
      { type: 'drag-select', fromIndex: 0, toIndex: 4, additive: false },
      [],
    );
    expect(ids(state)).toEqual([]);
  });
});

describe('selectionReducer — focus movement', () => {
  it('ArrowDown from no focus lands on the first row and selects it', () => {
    const state = run([{ type: 'focus-move', key: 'ArrowDown', extend: false, preserve: false }]);
    expect(state.focusedId).toBe('a');
    expect(ids(state)).toEqual(['a']);
  });

  it('ArrowDown moves one row and replaces the selection', () => {
    const state = run([
      click('b', PLAIN),
      { type: 'focus-move', key: 'ArrowDown', extend: false, preserve: false },
    ]);
    expect(state.focusedId).toBe('c');
    expect(ids(state)).toEqual(['c']);
  });

  it('ArrowDown stops at the last row instead of wrapping', () => {
    const state = run([
      click('e', PLAIN),
      { type: 'focus-move', key: 'ArrowDown', extend: false, preserve: false },
    ]);
    expect(state.focusedId).toBe('e');
  });

  it('ArrowUp stops at the first row instead of wrapping', () => {
    const state = run([
      click('a', PLAIN),
      { type: 'focus-move', key: 'ArrowUp', extend: false, preserve: false },
    ]);
    expect(state.focusedId).toBe('a');
  });

  it('Home focuses the first row and End the last', () => {
    const home = run([click('c', PLAIN), { type: 'focus-move', key: 'Home', extend: false, preserve: false }]);
    expect(home.focusedId).toBe('a');
    const end = run([click('c', PLAIN), { type: 'focus-move', key: 'End', extend: false, preserve: false }]);
    expect(end.focusedId).toBe('e');
  });

  it('shift+End extends the selection from the anchor to the last row', () => {
    const state = run([
      click('c', PLAIN),
      { type: 'focus-move', key: 'End', extend: true, preserve: false },
    ]);
    expect(ids(state)).toEqual(['c', 'd', 'e']);
    expect(state.anchorId).toBe('c');
  });

  it('shift+ArrowUp shrinks the range back towards the anchor', () => {
    const state = run([
      click('b', PLAIN),
      { type: 'focus-move', key: 'End', extend: true, preserve: false },
      { type: 'focus-move', key: 'ArrowUp', extend: true, preserve: false },
    ]);
    expect(ids(state)).toEqual(['b', 'c', 'd']);
  });

  it('End without shift collapses to one row (the negative twin of shift+End)', () => {
    const state = run([
      click('c', PLAIN),
      { type: 'focus-move', key: 'End', extend: false, preserve: false },
    ]);
    expect(ids(state)).toEqual(['e']);
  });

  it('accel+ArrowDown moves the focus and leaves the selection untouched', () => {
    const state = run([
      click('b', PLAIN),
      { type: 'focus-move', key: 'ArrowDown', extend: false, preserve: true },
    ]);
    expect(state.focusedId).toBe('c');
    expect(ids(state)).toEqual(['b']);
    expect(state.anchorId).toBe('b');
  });

  it('preserve wins over extend so accel+shift never rewrites the selection', () => {
    const state = run([
      click('b', PLAIN),
      { type: 'focus-move', key: 'End', extend: true, preserve: true },
    ]);
    expect(state.focusedId).toBe('e');
    expect(ids(state)).toEqual(['b']);
  });

  it('focus movement in an empty folder leaves the state alone', () => {
    const state = selectionReducer(
      emptySelection,
      { type: 'focus-move', key: 'End', extend: false, preserve: false },
      [],
    );
    expect(state).toBe(emptySelection);
  });
});

describe('reconcileSelection — rows that leave the listing', () => {
  const after = (state: SelectionState, gone: readonly string[]) =>
    reconcileSelection(state, ORDER, ORDER.filter((id) => !gone.includes(id)));

  it('moves the focus and the selection to the row below the one that went', () => {
    // The anchor is deliberately elsewhere: it is what the selection used to
    // snap back to when the focused row was deleted.
    const state = run([click('a', PLAIN), click('c', SHIFT), click('c', ACCEL)]);
    const next = after({ ...state, focusedId: 'c', anchorId: 'a' }, ['c']);
    expect(next.focusedId).toBe('d');
    expect(ids(next)).toEqual(['d']);
    expect(next.anchorId).toBe('d');
  });

  it('takes the row above when the last one went', () => {
    const next = after(run([click('e', PLAIN)]), ['e']);
    expect(next.focusedId).toBe('d');
    expect(ids(next)).toEqual(['d']);
  });

  it('skips a neighbour that went in the same sweep', () => {
    const next = after(run([click('b', PLAIN)]), ['b', 'c', 'd']);
    expect(next.focusedId).toBe('e');
  });

  it('leaves a listing where the focused row survived exactly as it was', () => {
    const state = run([click('b', PLAIN)]);
    expect(after(state, ['d'])).toBe(state);
  });

  it('drops rows that went from a multi-row selection without moving the focus', () => {
    const state = run([click('b', PLAIN), click('d', ACCEL)]);
    const next = after(state, ['b']);
    expect(ids(next)).toEqual(['d']);
    expect(next.focusedId).toBe('d');
  });

  it('focuses nothing when every row went', () => {
    const next = reconcileSelection(run([click('c', PLAIN)]), ORDER, []);
    expect(next.focusedId).toBeNull();
    expect(ids(next)).toEqual([]);
  });

  it('picks no successor when the listing was replaced wholesale', () => {
    // Walking into another folder: nothing survived, so there is no neighbour —
    // selecting a row in a folder the person has only just arrived in would be
    // a selection they never made.
    const next = reconcileSelection(run([click('c', PLAIN)]), ORDER, ['x', 'y']);
    expect(next.focusedId).toBeNull();
    expect(ids(next)).toEqual([]);
  });
});

describe('describeSelection', () => {
  it.each([
    { actions: [] as SelectionAction[], expected: 'No items selected' },
    { actions: [click('b', PLAIN)], expected: '1 item selected' },
    { actions: [click('b', PLAIN), click('d', ACCEL)], expected: '2 items selected' },
  ])('announces $expected', ({ actions, expected }) => {
    expect(describeSelection(run(actions))).toBe(expected);
  });
});

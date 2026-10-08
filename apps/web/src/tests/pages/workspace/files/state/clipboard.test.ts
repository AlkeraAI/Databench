import { describe, expect, it } from 'vitest';

import {
  type ClipboardAction,
  type ClipboardItem,
  type ClipboardState,
  clipboardReducer,
  emptyClipboard,
  isCut,
  pastePlan,
} from '@/pages/workspace/files/state/clipboard';

/** One clipboard entry, as a cut of `id` records it. */
function entry(id: string): ClipboardItem {
  return { id, etag: `etag-${id}`, name: id };
}

function run(
  actions: readonly ClipboardAction[],
  initial: ClipboardState = emptyClipboard,
): ClipboardState {
  return actions.reduce(clipboardReducer, initial);
}

const copied = run([{ type: 'copy', items: [entry('n1'), entry('n2')], sourceFolderId: 'src' }]);
const cut = run([{ type: 'cut', items: [entry('n1')], sourceFolderId: 'src' }]);

describe('clipboardReducer — taking items', () => {
  it('copy records the items, the source folder and the mode', () => {
    expect(copied).toEqual({ mode: 'copy', items: [entry('n1'), entry('n2')], sourceFolderId: 'src' });
  });

  it('cut records the same shape with the cut mode', () => {
    expect(cut.mode).toBe('cut');
    expect(cut.items).toEqual([entry('n1')]);
  });

  it('a copy over a cut replaces the mode, so the pending move is abandoned', () => {
    const state = run([{ type: 'copy', items: [entry('n9')], sourceFolderId: 'other' }], cut);
    expect(state.mode).toBe('copy');
    expect(state.items).toEqual([entry('n9')]);
  });

  it('copying an empty selection leaves the clipboard alone', () => {
    const state = run([{ type: 'copy', items: [], sourceFolderId: 'src' }], cut);
    expect(state).toBe(cut);
  });

  it('the clipboard keeps its own copy of the items', () => {
    const source = [entry('n1'), entry('n2')];
    const state = run([{ type: 'copy', items: source, sourceFolderId: 'src' }]);
    source.push(entry('n3'));
    expect(state.items).toEqual([entry('n1'), entry('n2')]);
  });

  it('a cut records the etag each item had, which is what the paste is fenced on', () => {
    expect(cut.items.map((item) => item.etag)).toEqual(['etag-n1']);
  });
});

describe('pastePlan — a cut pastes as a move', () => {
  it('a copy pastes as a copy', () => {
    expect(pastePlan(copied, 'dst')).toEqual({
      operation: 'copy',
      items: [entry('n1'), entry('n2')],
      sourceFolderId: 'src',
      targetFolderId: 'dst',
    });
  });

  it('a cut pastes as a move', () => {
    expect(pastePlan(cut, 'dst')?.operation).toBe('move');
  });

  it('a cut pasted back into its own folder is refused', () => {
    expect(pastePlan(cut, 'src')).toBeNull();
  });

  it('a copy pasted into its own folder is allowed, since it duplicates', () => {
    expect(pastePlan(copied, 'src')?.operation).toBe('copy');
  });

  it('an empty clipboard has nothing to paste', () => {
    expect(pastePlan(emptyClipboard, 'dst')).toBeNull();
  });
});

describe('clipboardReducer — pasting', () => {
  it('a cut is cleared by the paste that consumed it', () => {
    const state = run([{ type: 'paste', targetFolderId: 'dst' }], cut);
    expect(state).toEqual(emptyClipboard);
    expect(pastePlan(state, 'dst2')).toBeNull();
  });

  it('a copy survives its paste so it can be pasted again', () => {
    const state = run([{ type: 'paste', targetFolderId: 'dst' }], copied);
    expect(state).toEqual(copied);
    expect(pastePlan(state, 'dst2')?.operation).toBe('copy');
  });

  it('a refused paste keeps the cut on the clipboard', () => {
    const state = run([{ type: 'paste', targetFolderId: 'src' }], cut);
    expect(state).toEqual(cut);
  });

  it('pasting an empty clipboard is a no-op', () => {
    const state = run([{ type: 'paste', targetFolderId: 'dst' }]);
    expect(state).toBe(emptyClipboard);
  });

  it('clear empties a cut without performing it', () => {
    const state = run([{ type: 'clear' }], cut);
    expect(state).toEqual(emptyClipboard);
  });

  it('clear on an empty clipboard changes nothing', () => {
    expect(run([{ type: 'clear' }])).toBe(emptyClipboard);
  });
});

describe('isCut — the dimmed row state', () => {
  it('marks the cut items and nothing else', () => {
    expect(isCut(cut, 'n1')).toBe(true);
    expect(isCut(cut, 'n2')).toBe(false);
  });

  it('never marks copied items', () => {
    expect(isCut(copied, 'n1')).toBe(false);
  });

  it('stops marking once the cut has been pasted', () => {
    const state = run([{ type: 'paste', targetFolderId: 'dst' }], cut);
    expect(isCut(state, 'n1')).toBe(false);
  });
});

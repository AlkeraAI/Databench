/**
 * Copy / cut / paste state for the Files browser.
 *
 * A cut pastes as a move and is consumed by that paste; a copy stays on the
 * clipboard so it can be pasted into several folders.
 */

export type ClipboardMode = 'copy' | 'cut';

/**
 * What a paste needs to know about one item. A paste happens in a folder the
 * item is no longer listed in, so nothing there can look these up: the write is
 * fenced on the version the item was cut at, and the report names it.
 */
export interface ClipboardItem {
  readonly id: string;
  readonly etag: string;
  readonly name: string;
}

export interface ClipboardState {
  readonly mode: ClipboardMode | null;
  readonly items: readonly ClipboardItem[];
  readonly sourceFolderId: string | null;
}

export type ClipboardAction =
  | {
      readonly type: 'copy';
      readonly items: readonly ClipboardItem[];
      readonly sourceFolderId: string;
    }
  | {
      readonly type: 'cut';
      readonly items: readonly ClipboardItem[];
      readonly sourceFolderId: string;
    }
  | { readonly type: 'paste'; readonly targetFolderId: string }
  | { readonly type: 'clear' };

/** What a paste would do; `null` when the paste is a no-op. */
export interface PastePlan {
  readonly operation: 'copy' | 'move';
  readonly items: readonly ClipboardItem[];
  readonly sourceFolderId: string;
  readonly targetFolderId: string;
}

export const emptyClipboard: ClipboardState = {
  mode: null,
  items: [],
  sourceFolderId: null,
};

/**
 * The operation a paste into `targetFolderId` would perform. A cut pasted back
 * into the folder it came from would move an item onto itself, so it is
 * refused rather than issued as a no-op move.
 */
export function pastePlan(state: ClipboardState, targetFolderId: string): PastePlan | null {
  if (state.mode === null || state.items.length === 0 || state.sourceFolderId === null) {
    return null;
  }
  if (state.mode === 'cut' && state.sourceFolderId === targetFolderId) return null;
  return {
    operation: state.mode === 'cut' ? 'move' : 'copy',
    items: state.items,
    sourceFolderId: state.sourceFolderId,
    targetFolderId,
  };
}

export function clipboardReducer(state: ClipboardState, action: ClipboardAction): ClipboardState {
  switch (action.type) {
    case 'copy':
    case 'cut':
      if (action.items.length === 0) return state;
      return {
        mode: action.type,
        items: [...action.items],
        sourceFolderId: action.sourceFolderId,
      };
    case 'paste': {
      const plan = pastePlan(state, action.targetFolderId);
      if (plan === null) return state;
      // The cut is spent by the move it produced; a copy can be pasted again.
      return plan.operation === 'move' ? emptyClipboard : state;
    }
    case 'clear':
      return state.mode === null ? state : emptyClipboard;
  }
}

/** True when a row should render in the dimmed "cut" state. */
export function isCut(state: ClipboardState, itemId: string): boolean {
  return state.mode === 'cut' && state.items.some((item) => item.id === itemId);
}

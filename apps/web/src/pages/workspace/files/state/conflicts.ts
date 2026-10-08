/**
 * The upload conflict queue: one prompt at a time, with an "apply to the rest"
 * escape hatch so a 5,000-file drop is not 5,000 clicks.
 */

export type ConflictDecision = 'replace' | 'keep-both' | 'skip';

export interface Conflict {
  /** The upload tray item this conflict belongs to. */
  readonly id: string;
  readonly name: string;
  readonly targetFolderId: string;
}

export interface ResolvedConflict extends Conflict {
  readonly decision: ConflictDecision;
}

export interface ConflictsState {
  readonly pending: readonly Conflict[];
  readonly resolved: readonly ResolvedConflict[];
}

export type ConflictsAction =
  | { readonly type: 'enqueue'; readonly conflicts: readonly Conflict[] }
  | {
      readonly type: 'decide';
      readonly id: string;
      readonly decision: ConflictDecision;
      /** Apply the same decision to every conflict still pending. */
      readonly applyToRest: boolean;
    }
  | { readonly type: 'skip-rest' };

export const emptyConflicts: ConflictsState = { pending: [], resolved: [] };

export function conflictsReducer(state: ConflictsState, action: ConflictsAction): ConflictsState {
  switch (action.type) {
    case 'enqueue': {
      const known = new Set([
        ...state.pending.map((c) => c.id),
        ...state.resolved.map((c) => c.id),
      ]);
      const added = action.conflicts.filter((c) => !known.has(c.id));
      return added.length === 0 ? state : { ...state, pending: [...state.pending, ...added] };
    }
    case 'decide': {
      const target = state.pending.find((c) => c.id === action.id);
      // A decision for something that is not waiting is a stale click.
      if (target === undefined) return state;
      if (action.applyToRest) {
        return {
          pending: [],
          resolved: [
            ...state.resolved,
            ...state.pending.map((c) => ({ ...c, decision: action.decision })),
          ],
        };
      }
      return {
        pending: state.pending.filter((c) => c.id !== action.id),
        resolved: [...state.resolved, { ...target, decision: action.decision }],
      };
    }
    case 'skip-rest': {
      if (state.pending.length === 0) return state;
      return {
        pending: [],
        resolved: [
          ...state.resolved,
          ...state.pending.map<ResolvedConflict>((c) => ({ ...c, decision: 'skip' })),
        ],
      };
    }
  }
}

/** The conflict the prompt is showing, or `null` when the queue is drained. */
export function currentConflict(state: ConflictsState): Conflict | null {
  return state.pending[0] ?? null;
}

export function decisionFor(state: ConflictsState, id: string): ConflictDecision | null {
  return state.resolved.find((c) => c.id === id)?.decision ?? null;
}

/** The ids the uploader should still send, in queue order. */
export function acceptedIds(state: ConflictsState): readonly string[] {
  return state.resolved.filter((c) => c.decision !== 'skip').map((c) => c.id);
}

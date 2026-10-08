import { describe, expect, it } from 'vitest';

import {
  type Conflict,
  type ConflictsAction,
  type ConflictsState,
  acceptedIds,
  conflictsReducer,
  currentConflict,
  decisionFor,
  emptyConflicts,
} from '@/pages/workspace/files/state/conflicts';

const CONFLICTS: readonly Conflict[] = [
  { id: 'f1', name: 'notes.md', targetFolderId: 'dst' },
  { id: 'f2', name: 'photo.png', targetFolderId: 'dst' },
  { id: 'f3', name: 'data.csv', targetFolderId: 'dst' },
];

function run(
  actions: readonly ConflictsAction[],
  initial: ConflictsState = emptyConflicts,
): ConflictsState {
  return actions.reduce(conflictsReducer, initial);
}

const queued = run([{ type: 'enqueue', conflicts: CONFLICTS }]);

describe('conflictsReducer — the queue', () => {
  it('prompts for the first conflict in queue order', () => {
    expect(currentConflict(queued)?.id).toBe('f1');
  });

  it('an empty queue has nothing to prompt for', () => {
    expect(currentConflict(emptyConflicts)).toBeNull();
  });

  it('enqueuing the same conflict twice does not double the queue', () => {
    const state = run([{ type: 'enqueue', conflicts: CONFLICTS }], queued);
    expect(state.pending).toHaveLength(3);
  });

  it('a conflict already decided is not asked about again', () => {
    const decided = run(
      [{ type: 'decide', id: 'f1', decision: 'skip', applyToRest: false }],
      queued,
    );
    const state = run([{ type: 'enqueue', conflicts: [CONFLICTS[0]!] }], decided);
    expect(state.pending.map((c) => c.id)).toEqual(['f2', 'f3']);
  });
});

describe('conflictsReducer — one decision at a time', () => {
  it.each(['replace', 'keep-both', 'skip'] as const)('%s resolves just that conflict', (decision) => {
    const state = run([{ type: 'decide', id: 'f1', decision, applyToRest: false }], queued);
    expect(decisionFor(state, 'f1')).toBe(decision);
    expect(state.pending.map((c) => c.id)).toEqual(['f2', 'f3']);
    expect(currentConflict(state)?.id).toBe('f2');
  });

  it('a decision carries the conflict’s name and target so the caller can rename', () => {
    const state = run([{ type: 'decide', id: 'f2', decision: 'keep-both', applyToRest: false }], queued);
    expect(state.resolved).toEqual([
      { id: 'f2', name: 'photo.png', targetFolderId: 'dst', decision: 'keep-both' },
    ]);
  });

  it('a decision for a conflict that is not pending is ignored', () => {
    expect(run([{ type: 'decide', id: 'nope', decision: 'replace', applyToRest: false }], queued)).toBe(
      queued,
    );
  });

  it('a second decision for an already-resolved conflict does not overwrite the first', () => {
    const state = run(
      [
        { type: 'decide', id: 'f1', decision: 'replace', applyToRest: false },
        { type: 'decide', id: 'f1', decision: 'skip', applyToRest: false },
      ],
      queued,
    );
    expect(decisionFor(state, 'f1')).toBe('replace');
    expect(state.resolved).toHaveLength(1);
  });

  it('a conflict still pending has no decision yet', () => {
    expect(decisionFor(queued, 'f1')).toBeNull();
  });
});

describe('conflictsReducer — apply to the rest', () => {
  it.each(['replace', 'keep-both', 'skip'] as const)(
    '%s applied to the rest drains the queue with that decision',
    (decision) => {
      const state = run([{ type: 'decide', id: 'f1', decision, applyToRest: true }], queued);
      expect(state.pending).toHaveLength(0);
      expect(state.resolved.map((c) => c.decision)).toEqual([decision, decision, decision]);
    },
  );

  it('apply-to-the-rest never touches a conflict decided before it', () => {
    const state = run(
      [
        { type: 'decide', id: 'f1', decision: 'replace', applyToRest: false },
        { type: 'decide', id: 'f2', decision: 'skip', applyToRest: true },
      ],
      queued,
    );
    expect(decisionFor(state, 'f1')).toBe('replace');
    expect(decisionFor(state, 'f3')).toBe('skip');
  });

  it('without apply-to-the-rest the other conflicts stay pending (the negative twin)', () => {
    const state = run([{ type: 'decide', id: 'f1', decision: 'skip', applyToRest: false }], queued);
    expect(state.pending).toHaveLength(2);
  });
});

describe('conflictsReducer — skipping the rest', () => {
  it('skip-rest records every pending conflict as skipped', () => {
    const state = run([{ type: 'skip-rest' }], queued);
    expect(state.pending).toHaveLength(0);
    expect(state.resolved.map((c) => c.decision)).toEqual(['skip', 'skip', 'skip']);
  });

  it('skip-rest on a drained queue changes nothing', () => {
    const drained = run([{ type: 'skip-rest' }], queued);
    expect(run([{ type: 'skip-rest' }], drained)).toBe(drained);
  });
});

describe('acceptedIds — what the uploader still sends', () => {
  it('keeps replace and keep-both and drops skip, in queue order', () => {
    const state = run(
      [
        { type: 'decide', id: 'f1', decision: 'replace', applyToRest: false },
        { type: 'decide', id: 'f2', decision: 'skip', applyToRest: false },
        { type: 'decide', id: 'f3', decision: 'keep-both', applyToRest: false },
      ],
      queued,
    );
    expect(acceptedIds(state)).toEqual(['f1', 'f3']);
  });

  it('is empty when everything was skipped', () => {
    expect(acceptedIds(run([{ type: 'skip-rest' }], queued))).toEqual([]);
  });
});

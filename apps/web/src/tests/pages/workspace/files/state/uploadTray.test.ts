import { describe, expect, it } from 'vitest';

import {
  type UploadItem,
  type UploadStatus,
  type UploadTrayAction,
  type UploadTrayState,
  describeDedup,
  emptyUploadTray,
  trayProgress,
  uploadTrayReducer,
} from '@/pages/workspace/files/state/uploadTray';

function run(
  actions: readonly UploadTrayAction[],
  initial: UploadTrayState = emptyUploadTray,
): UploadTrayState {
  return actions.reduce(uploadTrayReducer, initial);
}

const ENQUEUE: UploadTrayAction = {
  type: 'enqueue',
  files: [
    { id: 'f1', name: 'notes.md', size: 100 },
    { id: 'f2', name: 'photo.png', size: 300 },
  ],
};

function item(state: UploadTrayState, id: string): UploadItem {
  const found = state.items.find((row) => row.id === id);
  if (found === undefined) throw new Error(`no tray row ${id}`);
  return found;
}

describe('uploadTrayReducer — queueing', () => {
  it('enqueued files start queued with no bytes sent', () => {
    const state = run([ENQUEUE]);
    expect(state.items.map((row) => row.status)).toEqual<UploadStatus[]>(['queued', 'queued']);
    expect(item(state, 'f1')).toMatchObject({ name: 'notes.md', uploaded: 0, deduped: false });
  });

  it('re-enqueuing a file already in the tray does not duplicate the row', () => {
    const state = run([ENQUEUE, ENQUEUE]);
    expect(state.items).toHaveLength(2);
  });

  it('enqueue keeps the rows already there and appends the new one', () => {
    const state = run([ENQUEUE, { type: 'enqueue', files: [{ id: 'f3', name: 'a.csv', size: 10 }] }]);
    expect(state.items.map((row) => row.id)).toEqual(['f1', 'f2', 'f3']);
  });
});

describe('uploadTrayReducer — running, pausing and resuming', () => {
  const started = run([ENQUEUE, { type: 'start', id: 'f1' }]);

  it('start moves a queued row to uploading', () => {
    expect(item(started, 'f1').status).toBe('uploading');
  });

  it('progress advances the bytes while uploading', () => {
    const state = run([{ type: 'progress', id: 'f1', uploaded: 40 }], started);
    expect(item(state, 'f1').uploaded).toBe(40);
  });

  it('progress is clamped to the file size', () => {
    const state = run([{ type: 'progress', id: 'f1', uploaded: 999 }], started);
    expect(item(state, 'f1').uploaded).toBe(100);
  });

  it('progress on a queued row is ignored', () => {
    const state = run([ENQUEUE, { type: 'progress', id: 'f2', uploaded: 50 }]);
    expect(item(state, 'f2').uploaded).toBe(0);
  });

  it('a late progress callback cannot move a paused row (the negative twin of progress)', () => {
    const state = run(
      [
        { type: 'progress', id: 'f1', uploaded: 40 },
        { type: 'pause', id: 'f1' },
        { type: 'progress', id: 'f1', uploaded: 90 },
      ],
      started,
    );
    expect(item(state, 'f1').uploaded).toBe(40);
  });

  it('resume keeps the bytes already accepted instead of restarting', () => {
    const state = run(
      [
        { type: 'progress', id: 'f1', uploaded: 40 },
        { type: 'pause', id: 'f1' },
        { type: 'resume', id: 'f1' },
      ],
      started,
    );
    expect(item(state, 'f1')).toMatchObject({ status: 'uploading', uploaded: 40 });
  });

  it('pause holds a queued row so it is not picked up', () => {
    const state = run([ENQUEUE, { type: 'pause', id: 'f2' }]);
    expect(item(state, 'f2').status).toBe('paused');
  });

  it('resume on a row that was never paused does nothing', () => {
    const state = run([{ type: 'resume', id: 'f1' }], started);
    expect(state).toBe(started);
  });

  it('start on an already-uploading row does nothing', () => {
    const state = run([{ type: 'start', id: 'f1' }], started);
    expect(state).toBe(started);
  });

  it('an action for an unknown row leaves the tray untouched', () => {
    const state = run([{ type: 'progress', id: 'nope', uploaded: 5 }], started);
    expect(state).toBe(started);
  });
});

describe('uploadTrayReducer — finishing, failing and retrying', () => {
  const started = run([ENQUEUE, { type: 'start', id: 'f1' }]);

  it('complete fills the bar and marks the row done', () => {
    const state = run([{ type: 'complete', id: 'f1', deduped: false }], started);
    expect(item(state, 'f1')).toMatchObject({ status: 'done', uploaded: 100 });
  });

  it('a done row cannot be paused, started or failed afterwards', () => {
    const done = run([{ type: 'complete', id: 'f1', deduped: false }], started);
    expect(run([{ type: 'pause', id: 'f1' }], done)).toBe(done);
    expect(run([{ type: 'start', id: 'f1' }], done)).toBe(done);
    expect(run([{ type: 'fail', id: 'f1', error: 'late' }], done)).toBe(done);
  });

  it('fail records the reason and keeps the bytes already sent', () => {
    const state = run(
      [
        { type: 'progress', id: 'f1', uploaded: 60 },
        { type: 'fail', id: 'f1', error: 'The connection dropped.' },
      ],
      started,
    );
    expect(item(state, 'f1')).toMatchObject({
      status: 'failed',
      uploaded: 60,
      error: 'The connection dropped.',
    });
  });

  it('retry re-queues a failed row from zero and drops the reason', () => {
    const state = run(
      [
        { type: 'progress', id: 'f1', uploaded: 60 },
        { type: 'fail', id: 'f1', error: 'The connection dropped.' },
        { type: 'retry', id: 'f1' },
      ],
      started,
    );
    expect(item(state, 'f1')).toMatchObject({ status: 'queued', uploaded: 0, error: null });
  });

  it('retry on a row that did not fail does nothing', () => {
    expect(run([{ type: 'retry', id: 'f1' }], started)).toBe(started);
  });

  it('cancel removes the row from the tray', () => {
    const state = run([{ type: 'cancel', id: 'f1' }], started);
    expect(state.items.map((row) => row.id)).toEqual(['f2']);
  });

  it('cancelling an unknown row leaves the tray identical', () => {
    expect(run([{ type: 'cancel', id: 'nope' }], started)).toBe(started);
  });

  it('clear-finished keeps everything that is still going', () => {
    const state = run(
      [
        { type: 'complete', id: 'f1', deduped: false },
        { type: 'clear-finished' },
      ],
      started,
    );
    expect(state.items.map((row) => row.id)).toEqual(['f2']);
  });
});

describe('trayProgress', () => {
  it('sums the bytes across the rows and reports whole percent', () => {
    const state = run([ENQUEUE, { type: 'start', id: 'f1' }, { type: 'progress', id: 'f1', uploaded: 100 }]);
    expect(trayProgress(state)).toMatchObject({ uploadedBytes: 100, totalBytes: 400, percent: 25 });
  });

  it('counts the rows still to finish and the failed ones separately', () => {
    const state = run([
      ENQUEUE,
      { type: 'start', id: 'f1' },
      { type: 'fail', id: 'f1', error: 'boom' },
    ]);
    expect(trayProgress(state)).toMatchObject({ active: 1, failed: 1 });
  });

  it('an empty tray is 0 percent, not a division by zero', () => {
    expect(trayProgress(emptyUploadTray).percent).toBe(0);
  });

  it('zero-byte files report 0 percent rather than NaN', () => {
    const state = run([{ type: 'enqueue', files: [{ id: 'e', name: 'empty', size: 0 }] }]);
    expect(trayProgress(state).percent).toBe(0);
  });
});

describe('describeDedup — "n of m already in Files"', () => {
  it('counts only the finished rows whose content was already stored', () => {
    const state = run([
      ENQUEUE,
      { type: 'enqueue', files: [{ id: 'f3', name: 'c.txt', size: 5 }] },
      { type: 'complete', id: 'f1', deduped: true },
      { type: 'complete', id: 'f2', deduped: false },
      { type: 'complete', id: 'f3', deduped: true },
    ]);
    expect(describeDedup(state)).toBe('2 of 3 already in Files');
  });

  it('says nothing when every byte had to be sent', () => {
    const state = run([ENQUEUE, { type: 'complete', id: 'f1', deduped: false }]);
    expect(describeDedup(state)).toBeNull();
  });

  it('ignores rows that are still uploading', () => {
    const state = run([
      ENQUEUE,
      { type: 'complete', id: 'f1', deduped: true },
      { type: 'start', id: 'f2' },
    ]);
    expect(describeDedup(state)).toBe('1 of 1 already in Files');
  });
});

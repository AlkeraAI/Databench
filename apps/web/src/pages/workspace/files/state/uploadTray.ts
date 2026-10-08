/**
 * Upload tray state: one row per file, with the transitions the tray's
 * pause / resume / cancel / retry controls are allowed to make.
 */

export type UploadStatus = 'queued' | 'uploading' | 'paused' | 'done' | 'failed';

export interface UploadItem {
  readonly id: string;
  readonly name: string;
  readonly size: number;
  /** Bytes accepted by the server so far. */
  readonly uploaded: number;
  readonly status: UploadStatus;
  /** Product-copy reason shown on the row when the upload failed. */
  readonly error: string | null;
  /** The content was already stored, so no bytes were sent. */
  readonly deduped: boolean;
}

export interface UploadTrayState {
  readonly items: readonly UploadItem[];
}

export type UploadTrayAction =
  | {
      readonly type: 'enqueue';
      readonly files: readonly { readonly id: string; readonly name: string; readonly size: number }[];
    }
  | { readonly type: 'start'; readonly id: string }
  | { readonly type: 'progress'; readonly id: string; readonly uploaded: number }
  | { readonly type: 'pause'; readonly id: string }
  | { readonly type: 'resume'; readonly id: string }
  | { readonly type: 'complete'; readonly id: string; readonly deduped: boolean }
  | { readonly type: 'fail'; readonly id: string; readonly error: string }
  | { readonly type: 'retry'; readonly id: string }
  | { readonly type: 'cancel'; readonly id: string }
  | { readonly type: 'clear-finished' };

export const emptyUploadTray: UploadTrayState = { items: [] };

function mapItem(
  state: UploadTrayState,
  id: string,
  change: (item: UploadItem) => UploadItem,
): UploadTrayState {
  let changed = false;
  const items = state.items.map((item) => {
    if (item.id !== id) return item;
    const next = change(item);
    if (next !== item) changed = true;
    return next;
  });
  return changed ? { items } : state;
}

export function uploadTrayReducer(
  state: UploadTrayState,
  action: UploadTrayAction,
): UploadTrayState {
  switch (action.type) {
    case 'enqueue': {
      const known = new Set(state.items.map((item) => item.id));
      const added = action.files
        .filter((file) => !known.has(file.id))
        .map<UploadItem>((file) => ({
          id: file.id,
          name: file.name,
          size: file.size,
          uploaded: 0,
          status: 'queued',
          error: null,
          deduped: false,
        }));
      return added.length === 0 ? state : { items: [...state.items, ...added] };
    }
    case 'start':
      return mapItem(state, action.id, (item) =>
        item.status === 'queued' ? { ...item, status: 'uploading', error: null } : item,
      );
    case 'progress':
      // Bytes only move while the transfer is actually running, so a late
      // callback cannot advance a paused or cancelled row's bar.
      return mapItem(state, action.id, (item) =>
        item.status === 'uploading'
          ? { ...item, uploaded: Math.max(0, Math.min(item.size, action.uploaded)) }
          : item,
      );
    case 'pause':
      return mapItem(state, action.id, (item) =>
        item.status === 'uploading' || item.status === 'queued'
          ? { ...item, status: 'paused' }
          : item,
      );
    case 'resume':
      // Resume keeps the bytes already accepted; it is not a restart.
      return mapItem(state, action.id, (item) =>
        item.status === 'paused' ? { ...item, status: 'uploading' } : item,
      );
    case 'complete':
      return mapItem(state, action.id, (item) =>
        item.status === 'done'
          ? item
          : { ...item, status: 'done', uploaded: item.size, error: null, deduped: action.deduped },
      );
    case 'fail':
      return mapItem(state, action.id, (item) =>
        item.status === 'done' ? item : { ...item, status: 'failed', error: action.error },
      );
    case 'retry':
      return mapItem(state, action.id, (item) =>
        item.status === 'failed'
          ? { ...item, status: 'queued', uploaded: 0, error: null }
          : item,
      );
    case 'cancel': {
      const items = state.items.filter((item) => item.id !== action.id);
      return items.length === state.items.length ? state : { items };
    }
    case 'clear-finished': {
      const items = state.items.filter((item) => item.status !== 'done');
      return items.length === state.items.length ? state : { items };
    }
  }
}

export interface TrayProgress {
  readonly uploadedBytes: number;
  readonly totalBytes: number;
  /** Whole percent, 0 when there is nothing to upload. */
  readonly percent: number;
  readonly active: number;
  readonly failed: number;
}

export function trayProgress(state: UploadTrayState): TrayProgress {
  let uploadedBytes = 0;
  let totalBytes = 0;
  let active = 0;
  let failed = 0;
  for (const item of state.items) {
    uploadedBytes += item.uploaded;
    totalBytes += item.size;
    if (item.status === 'queued' || item.status === 'uploading' || item.status === 'paused') {
      active += 1;
    }
    if (item.status === 'failed') failed += 1;
  }
  const percent = totalBytes === 0 ? 0 : Math.floor((uploadedBytes / totalBytes) * 100);
  return { uploadedBytes, totalBytes, percent, active, failed };
}

/** "n of m already in Files", or `null` when nothing was deduplicated. */
export function describeDedup(state: UploadTrayState): string | null {
  const finished = state.items.filter((item) => item.status === 'done');
  const deduped = finished.filter((item) => item.deduped).length;
  if (deduped === 0) return null;
  return `${deduped} of ${finished.length} already in Files`;
}

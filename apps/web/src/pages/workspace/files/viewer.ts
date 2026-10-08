// Opening an artifact's bytes instead of saving them.
//
// A PDF an agent left in Files is a deliverable: the reader double-clicks it to
// READ it, not to put a copy in ~/Downloads. The content route already serves
// the types below `inline`, each under a CSP of its own
// (`backend.content_app.csp_for`) and only when the SERVER sniffed the type —
// so the browser's own viewer renders it and nothing it contains can reach this
// origin. What was missing was a caller: the browser only ever built the
// attachment URL, and a double-click on a file did nothing at all.
//
// A type NOT on the list is not opened. The list is the server's, not a guess:
// asking for `disposition=inline` on anything else gets an attachment back
// anyway (the route decides from the sniffed type), so a tab would open on a
// download — worse than the row staying where it is.

import type { Item } from "@/api/files";

/** The sniffed types the content route will serve `inline`, mirroring
 *  `INLINE_MIME_TYPES` in `alkera_core/files/content.py`. `text/html` is on it
 *  because the content route serves a page under `sandbox` with no allowances
 *  and `default-src 'none'` (`backend.content_app.csp_for`): an opaque origin,
 *  no script, no forms, no top-level navigation, so there is nothing for stored
 *  XSS to read or submit. `image/svg+xml` renders under a stricter policy still,
 *  and the media types have no DOM to attack at all. The two lists are pinned
 *  against each other by `viewer.test.ts`, so neither can drift alone. */
export const VIEWABLE_MIME_TYPES: ReadonlySet<string> = new Set([
  "application/json",
  "application/pdf",
  "text/csv",
  "text/html",
  "text/plain",
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/svg+xml",
  "video/mp4",
  "video/webm",
  "audio/mpeg",
  "audio/wav",
]);

/** Where the browser sends a person to READ the bytes. `disposition=inline` is
 *  the explicit spelling of the route's own default; it is sent anyway so the
 *  intent is in the URL rather than in the absence of a parameter. */
export function viewerUrl(driveId: string, itemId: string): string {
  return `/api/v1/files/drives/${encodeURIComponent(driveId)}/items/${encodeURIComponent(itemId)}/content?disposition=inline`;
}

/** The URL that renders this row, or `null` when nothing renders it.
 *
 *  Keyed on the `file` facet's `mime_type`, which is what the server sniffed —
 *  never on the name's extension, which is what an uploader claimed. */
export function viewableUrl(item: Item): string | null {
  if (item.kind !== "file") return null;
  const mime = item.file?.mime_type;
  if (typeof mime !== "string") return null;
  // A sniffed type may carry parameters (`text/plain; charset=utf-8`).
  const essence = mime.split(";")[0]!.trim().toLowerCase();
  if (!VIEWABLE_MIME_TYPES.has(essence)) return null;
  const driveId = item.driveId;
  if (!driveId) return null;
  return viewerUrl(driveId, item.id);
}

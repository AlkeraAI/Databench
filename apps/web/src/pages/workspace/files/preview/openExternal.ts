// Opening a file whole, outside the preview.
//
// Two doors, and which one a file goes through is decided by the type the SERVER
// sniffed, never by its name:
//
//  * A document the browser renders itself — a page, a PDF — must open on the
//    content origin under its own grant. That origin serves it under a CSP of
//    its own with no script and no forms, so nothing inside it can reach the
//    app. Opening such a file on the app's origin is what "stored XSS" means.
//  * Everything else opens on the app's own inline content route, which is
//    authorized by the session and needs no grant at all.
//
// Both doors open with `noopener,noreferrer`: the new tab must not be able to
// navigate the one it came from, and the grant must not travel as a referrer.

import type { Item, MintContentGrant } from "@/api/files";

import { viewerUrl } from "../viewer";
import { mimeEssence } from "./usePreviewContent";

/** The types that render themselves and therefore open on the content origin. */
const PAGE_MIME_TYPES: ReadonlySet<string> = new Set(["text/html", "application/pdf"]);

const NEW_TAB = "noopener,noreferrer";

/** Open this row's bytes in a new tab. Rejects with the server's refusal when a
 *  grant it needed was not given — the tab is not opened on a guess. */
export async function openExternal(item: Item, mint: MintContentGrant): Promise<void> {
  if (item.kind !== "file") return;
  const driveId = item.driveId;
  if (!driveId) return;

  if (PAGE_MIME_TYPES.has(mimeEssence(item.file?.mime_type ?? ""))) {
    const grant = await mint({ driveId, itemId: item.id, kind: "page" });
    window.open(grant.url, "_blank", NEW_TAB);
    return;
  }
  window.open(viewerUrl(driveId, item.id), "_blank", NEW_TAB);
}

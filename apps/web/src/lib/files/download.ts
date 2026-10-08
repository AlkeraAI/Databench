// Getting a file's bytes onto the reader's disk.

import type { Item } from "@/api/files";

/** The content route for one node, asking for the bytes rather than a preview. */
export function contentUrl(driveId: string, itemId: string): string {
  return `/api/v1/files/drives/${encodeURIComponent(driveId)}/items/${encodeURIComponent(itemId)}/content?download=1`;
}

/** Download the bytes rather than navigate to them.
 *
 *  `window.open` gives a new tab, which a popup blocker swallows outright and
 *  which otherwise leaves the person looking at the file instead of holding it.
 *  A hidden same-origin anchor carrying `download` is the shape browsers turn
 *  into a save, and it cannot be blocked because the click is the person's own.
 *  `disposition=attachment` asks the content route for the matching
 *  `Content-Disposition`, so a type the browser would rather render inline is
 *  still saved; the anchor alone already suffices once the route sends it. */
export function downloadItem(url: string, item: Item): void {
  const anchor = document.createElement("a");
  anchor.href = `${url}${url.includes("?") ? "&" : "?"}disposition=attachment`;
  // The name the person sees in the listing is the name the file lands under.
  anchor.download = item.nameDisplay || item.name;
  anchor.rel = "noopener";
  anchor.hidden = true;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

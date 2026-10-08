import type { Item } from "@/api/files";

/**
 * Where a node that IS something else opens, or `null` for an ordinary one.
 *
 * Some rows in the tree are not their contents: a chat, a chat template, a saved
 * object. The server says so by hanging an `object` facet on the node carrying
 * the page that thing lives on, and a person who opens one expects the chat —
 * not a folder listing of the files the chat happens to keep, which is also why
 * those nodes refuse a download.
 *
 * The facet is read rather than the kind, because the same thing arrives in two
 * shapes: an `object` node, and a folder that carries a workspace object (a
 * `.alkerachat` directory). Either way the facet is what names the destination,
 * so neither shape needs its own branch — a template goes to `/templates/<id>`
 * for the same reason a chat goes to `/chat/<id>`. A same-origin absolute url is reduced
 * to its path so the SPA routes it instead of reloading itself; anything truly
 * elsewhere is left for the caller to open as an external link.
 */
export function objectRoute(item: Item): string | null {
  const url = item.object?.web_url;
  if (typeof url !== "string" || url === "") return null;
  if (url.startsWith("/")) return url;
  if (typeof window === "undefined") return null;
  try {
    const parsed = new URL(url, window.location.origin);
    if (parsed.origin !== window.location.origin) return null;
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return null;
  }
}

// Reaching the files stored beside a document being previewed.
//
// A markdown report writes `charts/q3.png`; the preview has to turn that into
// something an `<img>` can load. The rule is containment: the walk starts at the
// folder the document itself lives in and steps child by exact name. A path that
// climbs out, starts at the root, carries a backslash or a NUL, or names a
// folder as its leaf resolves to nothing and buys nothing — the client refuses
// it before the server ever hears about it, which is also what keeps a document
// from probing for names it cannot read.
//
// Each resolved file's bytes are bought with a grant of its own and handed back
// as an object URL. The grant is spent inside this module: what reaches the page
// is `blob:…`, never the URL that would let anyone else read the file.

import type { QueryClient } from "@tanstack/react-query";
import type { ChatFileRef, ChatFilesResolver } from "@alkera/ui";

import { api, request } from "@/api/client";
import {
  CHILDREN_PAGE,
  toChildrenParams,
  type ChildrenPage,
  type Item,
  type MintContentGrant,
} from "@/api/files";
import { keys } from "@/api/keys";
import { PREVIEW_FOLDER_WALK_PAGES, PREVIEW_PATH_MAX_SEGMENTS } from "@/lib/limits";

/** How deep a relative reference may go, and how many listings one walk may read
 *  from the server. A document naming a path deeper than this is not describing
 *  a file beside it. */
const MAX_SEGMENTS = PREVIEW_PATH_MAX_SEGMENTS;
/** A folder is read page by page; this bounds one walk's reads of a huge folder. */
const MAX_PAGES = PREVIEW_FOLDER_WALK_PAGES;

export interface FolderPreviewResolver extends ChatFilesResolver {
  /** The file a folder-relative path names, or null when nothing readable is
   *  there. */
  locate(path: string): Promise<ChatFileRef | null>;
  /** Release every object URL this resolver handed out. The host calls it when
   *  the preview goes away. */
  revokeAll(): void;
}

/** The segments of a folder-relative path, or null when the path is not one.
 *
 *  The same lexical rules the content route applies, applied here so a reference
 *  that could never resolve costs nothing: no empty, `.` or `..` segment (which
 *  rules out an absolute path, a doubled slash and a trailing slash), no
 *  backslash, no NUL, and a bounded depth. Nothing is normalized or case-folded
 *  — a name is compared as it is stored. */
function segmentsOf(path: string): string[] | null {
  if (!path || path.includes("\\") || path.includes("\0")) return null;
  const parts = path.split("/");
  if (parts.length > MAX_SEGMENTS) return null;
  for (const part of parts) {
    if (part === "" || part === "." || part === "..") return null;
  }
  return parts;
}

/** The rows of a folder the browser has already listed, whatever filters or page
 *  size it listed them under. */
function cachedRows(qc: QueryClient, driveId: string, parentId: string): Item[] {
  const rows: Item[] = [];
  for (const [, data] of qc.getQueriesData({
    queryKey: keys.files.childrenOf(driveId, parentId),
  })) {
    const pages = (data as { pages?: ChildrenPage[] } | undefined)?.pages;
    if (!pages) continue;
    for (const page of pages) rows.push(...page.value);
  }
  return rows;
}

async function serverRows(driveId: string, parentId: string): Promise<Item[]> {
  const rows: Item[] = [];
  let marker: string | undefined;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const answer = await request<ChildrenPage>(
      api.GET("/api/v1/files/drives/{drive_id}/items/{item_id}/children", {
        params: {
          path: { drive_id: driveId, item_id: parentId },
          query: {
            limit: CHILDREN_PAGE,
            ...toChildrenParams(),
            ...(marker ? { marker } : {}),
          },
        },
      }),
    );
    rows.push(...answer.value);
    marker = answer.nextMarker ?? undefined;
    if (!marker) break;
  }
  return rows;
}

async function childNamed(
  qc: QueryClient,
  driveId: string,
  parentId: string,
  name: string,
): Promise<Item | null> {
  const cached = cachedRows(qc, driveId, parentId).find((row) => row.name === name);
  if (cached) return cached;
  try {
    return (await serverRows(driveId, parentId)).find((row) => row.name === name) ?? null;
  } catch {
    // A folder the reader cannot list is indistinguishable from one that is not
    // there, which is the answer the server already gives.
    return null;
  }
}

/**
 * A resolver over one folder: what a preview of a document inside it may reach.
 *
 * `mint` is the host's door to the grant route and `qc` the browser's cache, so
 * a path the Files browser already listed costs no request at all.
 *
 * `onReveal` is where a reference the reader CLICKS goes. A document open in the
 * large preview names the files beside it; the reader clicking one means "show
 * me that instead", so the page selects its row in the listing behind and moves
 * the preview onto it — the same answer the Files tab beside a chat gives, on
 * the surface where the listing is the browser.
 */
export function folderResolver(
  folder: Item,
  mint: MintContentGrant,
  qc: QueryClient,
  onReveal?: (item: Item) => void,
): FolderPreviewResolver {
  const driveId = folder.driveId;
  const urls = new Map<string, string>();
  // What each located path turned out to be, and the folder the walk found it
  // in — `reveal` is handed the reference, not the row, and the page needs the
  // row. The containment comes from the walk rather than from the row, which is
  // the one place it is known for certain.
  const found = new Map<string, { item: Item; parentId: string }>();

  async function node(path: string): Promise<{ item: Item; parentId: string } | null> {
    const held = found.get(path);
    if (held) return held;
    const segments = segmentsOf(path);
    if (!segments || !driveId) return null;

    let at: Item = folder;
    for (const [index, segment] of segments.entries()) {
      const last = index === segments.length - 1;
      // Only the leaf may be a file; every step before it must be a folder, so a
      // path cannot walk "through" a file or out of the subtree.
      if (at.kind !== "folder") return null;
      const child = await childNamed(qc, driveId, at.id, segment);
      if (!child) return null;
      if (last) {
        if (child.kind !== "file") return null;
        const at_ = { item: child, parentId: at.id };
        found.set(path, at_);
        return at_;
      }
      at = child;
    }
    return null;
  }

  async function locate(path: string): Promise<ChatFileRef | null> {
    const at = await node(path);
    if (!at) return null;
    return { nodeId: at.item.id, parentId: at.parentId, name: at.item.name, path };
  }

  /** The reader clicked a reference. The row it names is already in hand from the
   *  walk that rendered the reference, so this costs no second lookup. */
  function reveal(ref: ChatFileRef): void {
    const at = found.get(ref.path);
    if (at) onReveal?.(at.item);
  }

  async function resolveUrl(path: string): Promise<string | null> {
    const held = urls.get(path);
    if (held) return held;
    const at = await node(path);
    if (!at || !driveId) return null;
    try {
      const grant = await mint({ driveId, itemId: at.item.id, kind: "file" });
      const response = await fetch(grant.url, {
        mode: "cors",
        credentials: "omit",
        cache: "no-store",
      });
      if (!response.ok) return null;
      const url = URL.createObjectURL(await response.blob());
      urls.set(path, url);
      return url;
    } catch {
      return null;
    }
  }

  function revokeAll(): void {
    for (const url of urls.values()) URL.revokeObjectURL(url);
    urls.clear();
  }

  return { locate, reveal, resolveUrl, revokeAll };
}

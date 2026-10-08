/**
 * What opening a Files row does. The one answer every gesture reads.
 *
 * A double-click, Enter, the context menu's first row, the details pane's
 * button, a search hit and "Open in new tab" all ask this module, so a row kind
 * cannot open one way from the keyboard and another from the menu. The answer
 * comes from the row's object facet first: the server names the page a chat,
 * a workspace, a template or a result lives on, and that page is what the
 * row IS. A folder-object whose deployment names no page still opens on its
 * files. Every other folder is listed, and every file goes to the surface's own
 * viewer (the preview on the Files page, a tab in a chat's Files pane).
 *
 * The page is never guessed on the client. A kind the server routes nowhere
 * useful is fixed in the server's route registry, where the on-disk pointer and
 * the object read take it from too.
 */

import type { Item } from "@/api/files";

import { folderObjectFilesTarget, folderObjectKindOf, isFolderObject } from "./chatFolder";
import { objectRoute } from "./objectRoute";

export type OpenTarget =
  /** The page the row is: a chat, a workspace, a template, a result. */
  | { readonly kind: "page"; readonly to: string }
  /** A folder to list, at the node named. */
  | { readonly kind: "folder"; readonly nodeId: string }
  /** A file, for the surface's own viewer. */
  | { readonly kind: "file" };

/** Where opening `item` goes. */
export function openTargetOf(item: Item): OpenTarget {
  const folderObject = folderObjectKindOf(item);
  if (folderObject?.opens === "files") {
    return { kind: "folder", nodeId: folderObjectFilesTarget(item) };
  }
  const page = objectRoute(item);
  if (page !== null) return { kind: "page", to: page };
  if (isFolderObject(item)) return { kind: "folder", nodeId: folderObjectFilesTarget(item) };
  if (item.kind === "folder") return { kind: "folder", nodeId: item.id };
  return { kind: "file" };
}

/** What the open row of a menu or a pane is called for this row. */
export function openLabelOf(item: Item | undefined): string {
  const door = pageDoorOf(item);
  if (door === null || folderObjectKindOf(item)?.opens !== "page") return "Open";
  return door.label;
}

/** The way to a folder-object's own page: what it is called and where it goes.
 *  `null` for an ordinary row, and for a folder-object whose deployment names
 *  no page. */
export interface PageDoor {
  readonly label: string;
  readonly to: string;
}

export function pageDoorOf(item: Item | undefined): PageDoor | null {
  const kind = folderObjectKindOf(item);
  if (kind === null || item === undefined) return null;
  const to = objectRoute(item);
  return to === null ? null : { label: kind.pageLabel, to };
}

/** The page door a row offers BESIDE Open: only where Open lists the files, so
 *  the page needs a way in of its own (a workspace). A chat's Open already is
 *  its page. */
export function secondPageDoorOf(item: Item | undefined): PageDoor | null {
  if (folderObjectKindOf(item)?.opens !== "files") return null;
  return pageDoorOf(item);
}

/** What the row that lists a folder-object's own files is called, on every
 *  surface: the menu, the details pane and the template page. */
export const BROWSE_FILES = "Browse files";

/** The folder "Browse files" lists for `item`: the working folder the server
 *  named on the facet (a chat's or a template's working directory, a
 *  workspace's shared `files/` tree), else the folder itself. */
export function browseTargetOf(item: Item): string {
  return folderObjectFilesTarget(item);
}

/** The address "Open in new tab" opens: the page, else the row's own listing. */
export function newTabHrefOf(item: Item): string {
  const target = openTargetOf(item);
  if (target.kind === "page") return target.to;
  if (target.kind === "folder") return `/files/${target.nodeId}`;
  return `/files/${item.id}`;
}

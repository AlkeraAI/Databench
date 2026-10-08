/**
 * Which rows a listing keeps out of sight, and the viewer's choice to see them.
 *
 * Two rules decide it, and this is the one place they meet:
 *
 * - System entries a person does not read: a name starting with "." and the
 *   folders a tool keeps its own state in (`__marimo__`, where a notebook's
 *   saved outputs live beside it). They are hidden at every depth, and the
 *   "Show hidden files" option brings them back, dimmed.
 * - A chat folder's box records (`hiddenInChatFolder`): the transcript, the
 *   decision log, the runtime directory. The option does not reveal these. They
 *   are the box's machinery for picking the chat up again, the transcript is
 *   already the conversation on the chat's page, and a person who edits one
 *   breaks the next box's replay rather than their own work.
 *
 * Only what a listing shows changes. A link, a breadcrumb or a search result that
 * names a hidden folder still opens it, because the server's listing is untouched.
 */

import { useCallback, useSyncExternalStore } from "react";

import type { Item } from "@/api/files";
import { safeLocalStorage } from "@alkera/ui/storage";

import { hiddenInChatFolder, isChatFolder } from "@/lib/files/chatFolder";

/** Folders a tool writes its own state into. Matched by exact name, and only on
 *  a folder: a file a person named `__marimo__` is theirs. */
export const SYSTEM_FOLDER_NAMES: ReadonlySet<string> = new Set(["__marimo__"]);

type Named = Pick<Item, "kind" | "name" | "nameDisplay">;

/** True for a row that is a system entry a person does not read. */
export function isSystemEntry(item: Named): boolean {
  const name = item.nameDisplay || item.name;
  if (name.startsWith(".")) return true;
  return item.kind === "folder" && SYSTEM_FOLDER_NAMES.has(name);
}

/** The chat-folder records rule for the folder being listed, or `undefined` for
 *  any other folder. Handed to a listing as its `omit`. */
export function chatRecordsRule(folder: Item | undefined): ((item: Item) => boolean) | undefined {
  if (!isChatFolder(folder)) return undefined;
  return (item: Item) => hiddenInChatFolder(item, folder);
}

/** How a row appears: as itself, dimmed because it is only shown on request, or
 *  not at all. */
export type EntryVisibility = "shown" | "dimmed" | "omitted";

export function entryVisibility(
  item: Item,
  omit: ((item: Item) => boolean) | undefined,
  showHidden: boolean,
): EntryVisibility {
  if (omit?.(item) === true) return "omitted";
  if (!isSystemEntry(item)) return "shown";
  return showHidden ? "dimmed" : "omitted";
}

/** The rows of a listing a person is shown. */
export function visibleRows(
  rows: readonly Item[],
  omit: ((item: Item) => boolean) | undefined,
  showHidden: boolean,
): Item[] {
  return rows.filter((row) => entryVisibility(row, omit, showHidden) !== "omitted");
}

/** One key for the viewer, not per drive: a person who wants to see dot files
 *  wants to see them everywhere. */
export const SHOW_HIDDEN_STORAGE_KEY = "alkera.files.showHidden";

const listeners = new Set<() => void>();

function readShowHidden(): boolean {
  return safeLocalStorage().get(SHOW_HIDDEN_STORAGE_KEY) === "true";
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Set the preference. A browser that refuses the write still switches for the
 *  rest of the page's life: the guarded store keeps refused writes in memory. */
export function writeShowHidden(next: boolean): void {
  safeLocalStorage().set(SHOW_HIDDEN_STORAGE_KEY, next ? "true" : "false");
  for (const listener of listeners) listener();
}

/** The viewer's "Show hidden files" choice, shared by every listing on the page
 *  so the browser and a pane counting its rows never disagree. Default off. */
export function useShowHiddenFiles(): [boolean, (next: boolean) => void] {
  const value = useSyncExternalStore(subscribe, readShowHidden, () => false);
  const set = useCallback((next: boolean) => writeShowHidden(next), []);
  return [value, set];
}

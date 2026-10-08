/**
 * A chat is two things at once, and this is the one place that says so.
 *
 * In the drive a chat is a real `<Title>.alkerachat` FOLDER: it has a page of
 * its own (the conversation) AND files a person can browse. Those files live in
 * the chat's working directory — the one directory its agent runs in, so what
 * a person hands it and what it produced are both in there — and the server
 * names that node on the chat's facet. Files opens a chat's files AT that node
 * and dresses it as the chat: the person sees the conversation's title on the
 * trail, never the directory's own name. Every other object-backed row is only
 * a page, so opening one can never be ambiguous; opening a chat can, which is
 * why the gesture asks instead of guessing.
 *
 * Sharing needs no branch here: a grant on the chat covers everything beneath
 * it, exactly as a grant on any folder does, so the files inside are ordinary
 * files — read, renamed, moved, downloaded and shared through the same routes.
 */

import type { Item } from "@/api/files";
import { objectRoute } from "./objectRoute";

/** The facet key under which the server names the node a chat's files live
 *  at. The client never derives it from a name: the directory's name is the
 *  box's business, and the one thing a person is never shown. */
export const CHAT_FILES_NODE_KEY = "files_node_id";

/** The node "Browse files" opens for a chat row, or `null` when the server named
 *  none — a chat from before it had a working directory, whose files then
 *  live at the chat folder itself. */
export function chatFilesNodeOf(item: Item | undefined): string | null {
  if (!isChatFolder(item) || item === undefined) return null;
  const named = item.object?.metadata?.[CHAT_FILES_NODE_KEY];
  return typeof named === "string" && named !== "" ? named : null;
}

/** True when `current` is the working directory of the chat `parent` — the
 *  node Files dresses as the chat itself. The identity is the server's, read
 *  off the chat's facet, so a sibling folder that merely shares the name is
 *  never mistaken for it. */
export function isChatFilesNode(current: Item | undefined, parent: Item | undefined): boolean {
  if (current === undefined || parent === undefined) return false;
  if (current.parentId !== parent.id) return false;
  return chatFilesNodeOf(parent) === current.id;
}

/** True when the row IS a chat and is also a real folder — the only node that
 *  has both a page to open and children to walk into.
 *
 *  The facet is read rather than the name: `.alkerachat` is a filesystem name
 *  minted once and never rewritten, and a person who renames a folder to end
 *  in it has not made a chat. */
export function isChatFolder(item: Item | undefined): boolean {
  if (item === undefined) return false;
  if (item.kind !== "folder") return false;
  if (item.object?.type !== "chat") return false;
  return objectRoute(item) !== null;
}

/**
 * Whether a child of the chat folder `chat` is kept out of a listing a person
 * reads.
 *
 * A chat folder holds two kinds of thing: the chat's work, in the working
 * directory the server names on the facet, and the box's own records beside it
 * — the transcript it replays, its decision log, the manifest that pins the
 * agent session, the runtime directory. The records travel through the drive
 * so the next box can pick the chat up; they are not a person's files, and the
 * transcript is already the conversation on the chat's page. So the rule is the
 * layout, not a list of names: where the chat names its working directory,
 * only that directory (and a folder an older chat kept its files in) is shown,
 * and every file and dot-entry beside it is the box's. A chat that names none
 * keeps its files at the folder itself; there only dot-entries are the box's.
 */
export function hiddenInChatFolder(item: Item, chat: Item | undefined): boolean {
  const name = item.nameDisplay || item.name;
  if (name.startsWith(".")) return true;
  const working = chatFilesNodeOf(chat);
  if (working === null) return false;
  if (item.id === working) return false;
  return item.kind !== "folder";
}

/** The object type a chat template's folder carries. */
export const TEMPLATE_OBJECT_TYPE = "chat_template";

/**
 * True when the row is a chat template: a real folder whose object facet says
 * so.
 *
 * A template is the second thing in the drive that is a page AND a folder of
 * files at once — the brief its author wrote, and the files a chat started from
 * it begins with. The facet is read rather than the name, for the same reason a
 * chat's is: `.alkerachat.template` is a filesystem name minted once, and a
 * person who types it onto a folder has not made a template.
 *
 * Unlike a chat this does not require the object to name a page: a template
 * whose page URL the deployment never configured is still a template, and the
 * things a person does with one here — start a chat from it, browse its files,
 * move it — need no page at all.
 */
export function isTemplateFolder(item: Item | undefined): boolean {
  if (item === undefined) return false;
  if (item.kind !== "folder") return false;
  return item.object?.type === TEMPLATE_OBJECT_TYPE;
}

/** The object type a workspace's folder carries: the place several chats share. */
export const WORKSPACE_OBJECT_TYPE = "workspace";

/** How a kind of folder-object is opened from a listing. */
export interface FolderObjectKind {
  /** What the way to its page is called. */
  readonly pageLabel: string;
  /** Where a double-click goes. A chat and a template are their page; a
   *  workspace is a place several chats keep files, so it opens like any
   *  folder and its page is the second way in. */
  readonly opens: "page" | "files";
}

/**
 * Every object type whose node is a real folder AND a page. These are the rows
 * with two ways in, the page and the files, and a new kind of folder-object is
 * a row here rather than a branch in every surface that opens one.
 */
export const FOLDER_OBJECT_KINDS: Readonly<Record<string, FolderObjectKind>> = {
  chat: { pageLabel: "Open chat", opens: "page" },
  [WORKSPACE_OBJECT_TYPE]: { pageLabel: "Open workspace", opens: "files" },
  [TEMPLATE_OBJECT_TYPE]: { pageLabel: "Open template", opens: "page" },
};

/** The folder-object kind the row is, read off the facet, or `null`. */
export function folderObjectKindOf(item: Item | undefined): FolderObjectKind | null {
  if (item === undefined || item.kind !== "folder") return null;
  const type = item.object?.type;
  if (type === undefined || !Object.hasOwn(FOLDER_OBJECT_KINDS, type)) return null;
  return FOLDER_OBJECT_KINDS[type] ?? null;
}

/** True when the row is a folder that is ALSO an object with a page of its own:
 *  a chat, a workspace or a chat template. Everything else in the drive already
 *  IS its own contents. Read off the facet, never the name. */
export function isFolderObject(item: Item | undefined): boolean {
  return folderObjectKindOf(item) !== null;
}

/** Where a template row's files are browsed: the working folder the server
 *  named on the facet, else the template folder itself. Same rule as a chat's,
 *  and for the same reason — the directory's name is the server's business. */
export function templateFilesTarget(item: Item): string {
  const named = item.object?.metadata?.[CHAT_FILES_NODE_KEY];
  return typeof named === "string" && named !== "" ? named : item.id;
}

/** Where a folder-object row's files are browsed: the working directory the
 *  server named on the facet, else the folder itself. One rule for every kind. */
export function folderObjectFilesTarget(item: Item): string {
  return templateFilesTarget(item);
}

/** The one sentence a template's listing says about itself: these files are not
 *  a copy of something, they are what a new chat starts with. */
export const TEMPLATE_FILES_NOTICE =
  "These are the template's files. A chat started from this template begins with a copy of them.";

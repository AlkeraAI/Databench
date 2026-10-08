// Where the shared chat chrome's keys lead, per shell.
//
// The chrome, the transcript and every card are the same components in both
// shells, so the paths they navigate to have to come from somewhere the shell
// decides; otherwise one shell's route table is hard-coded into a component the
// other one renders, and a key lands on the other shell's catch-all.
//
// So the targets live here, once, keyed off the host the shell installed. Both
// tables point at the SAME surfaces (`pages/workspace/chat/*`) — the extension
// mounts them as editor tabs under `/editor/*`, the portal as pages under the
// chat they came out of. Nothing is hidden: every key in both tables is routed
// by its shell (`webview/vscodeApp.tsx` and `App.tsx` respectively).

import type { BlobReference } from "@alkera/chat-model";
import type { ChromeSettingsId } from "@alkera/ui";

import { chatHost } from "./data";

/** Where one of the chrome's non-transcript keys leads.
 *
 *  Not every destination is a path: the editor opens knowledge and lineage as
 *  its own tabs through a command, the portal navigates to a page, and signing
 *  out is neither — it is the session the shell holds ending. `null` is the
 *  fourth answer and the important one: a shell with no such surface renders no
 *  key for it, rather than one that silently does nothing. */
export type ChromeAction =
  /** Navigate this shell's router to a page it declares. */
  | { kind: "route"; path: string }
  /** Dispatch an editor command through the host. */
  | { kind: "command"; command: string }
  /** End the session this shell holds. */
  | { kind: "sign-out" }
  /** Open the sharing dialog over the chat's own node, in place. Like
   *  signing out, this is not something a router can perform: the surface
   *  holds the dialog, so `performChromeAction` reports it undone. */
  | { kind: "share-dialog" };

export interface ChatRouteTable {
  /** Where a chat lands once it is deleted, and where New chat leads: the
   *  shell's chat home. Both shells need it, whether or not either renders a key. */
  home: string;
  /** Where the New chat key leads, and whether the shell carries one at all.
   *
   *  `null` in a browser tab: the portal's own nav already opens a new chat,
   *  and the header's account menu is likewise the app shell's job, so a chat
   *  page carries neither. The editor sidecar has no such nav, so it keeps the
   *  key. The home surface IS this destination, so it reads only the
   *  nullability here and answers the key by focusing its composer. */
  newChat: ChromeAction | null;
  /** A subagent's own transcript, drilled in from its spawn card. */
  subagent(childSessionId: string): string;
  /** What a compaction elided. */
  compaction(chatId: string, partId: string): string;
  /** A plan document, in full. */
  plan(chatId: string, partId: string): string;
  /** Every large result this chat produced. */
  results(chatId: string): string;
  /** One large result, paginated. */
  blob(chatId: string, reference: BlobReference): string;
  /** The workspace's knowledge, where this shell keeps it — `null` where it
   *  keeps none, and the chrome then carries no Knowledge key. */
  knowledge: ChromeAction | null;
  /** The lineage graph, likewise. The portal has no cloud lineage source yet
   *  (`App.tsx` deliberately routes no `/lineage`), so it renders no key rather
   *  than one that lands on "page not found". */
  lineage: ChromeAction | null;
  /** How this chat is SHARED.
   *
   *  A chat is a node in the drive — the object bridge gives every chat one —
   *  and sharing a chat is sharing that node, at the four rungs the file ladder
   *  already has. The chat's own row names the node (`files_node_id`), so the
   *  key opens the Files sharing dialog over it rather than dropping the reader
   *  at the drive's root to hunt for their chat.
   *
   *  `null` where the shell has no Files surface, which is the editor today. */
  share: ChromeAction | null;
  /** What the settings menu offers, in the package's own order. A shell lists
   *  only what it can actually perform: four editor commands in the extension,
   *  and in a browser tab the one action a page can honour. */
  settings: readonly { id: ChromeSettingsId; action: ChromeAction }[];
}

function blobQuery(reference: BlobReference): string {
  const query = new URLSearchParams();
  if (reference.name) query.set("name", reference.name);
  if (reference.refType) query.set("type", reference.refType);
  if (reference.mime) query.set("mime", reference.mime);
  const rendered = query.toString();
  return rendered ? `?${rendered}` : "";
}

/** The extension's route table: the stacked surfaces are editor tabs. */
const VSCODE: ChatRouteTable = {
  home: "/sidecar",
  newChat: { kind: "route", path: "/sidecar" },
  subagent: (childSessionId) => `/editor/chat/${childSessionId}`,
  compaction: (chatId, partId) =>
    `/editor/compaction/${encodeURIComponent(chatId)}/${encodeURIComponent(partId)}`,
  plan: (chatId, partId) =>
    `/editor/plan/${encodeURIComponent(chatId)}/${encodeURIComponent(partId)}`,
  results: (chatId) => `/editor/blobs/${encodeURIComponent(chatId)}`,
  blob: (chatId, reference) =>
    `/editor/blob/${encodeURIComponent(chatId)}/${encodeURIComponent(reference.handle)}${blobQuery(reference)}`,
  knowledge: { kind: "command", command: "alkera.openContext" },
  lineage: { kind: "command", command: "alkera.openLineage" },
  // The extension has no Files view yet, so there is nothing to lead to.
  share: null,
  settings: [
    { id: "plugins", action: { kind: "command", command: "alkera.openPlugins" } },
    { id: "jobs", action: { kind: "command", command: "alkera.openScheduler" } },
    { id: "preferences", action: { kind: "command", command: "alkera.openPreferences" } },
    { id: "logout", action: { kind: "command", command: "alkera.logout" } },
  ],
};

/** The portal's route table: the same surfaces, as pages under the chat they
 *  belong to, so a URL a reader can copy names the chat it came from. */
const BROWSER: ChatRouteTable = {
  // The empty composer, not the nav's `/chat` leaf: that leaf resolves to the
  // chat this account was last reading, so the hop home after deleting a chat
  // would land straight back on the one just thrown away.
  home: "/chat/new",
  // A portal page is not a window of its own: the app shell around it already
  // carries the nav that starts a chat, so a second New chat key in the chat's
  // own header is a duplicate.
  newChat: null,
  subagent: (childSessionId) => `/chat/${encodeURIComponent(childSessionId)}`,
  compaction: (chatId, partId) =>
    `/chat/${encodeURIComponent(chatId)}/compaction/${encodeURIComponent(partId)}`,
  plan: (chatId, partId) =>
    `/chat/${encodeURIComponent(chatId)}/plan/${encodeURIComponent(partId)}`,
  results: (chatId) => `/chat/${encodeURIComponent(chatId)}/results`,
  blob: (chatId, reference) =>
    `/chat/${encodeURIComponent(chatId)}/result/${encodeURIComponent(reference.handle)}${blobQuery(reference)}`,
  // The Knowledge key is off the chat header for now. The portal's knowledge
  // page is still there and still in the nav; what it is not is a key beside
  // the chat's title. `null` is how a shell says it offers no such key, and it
  // also stops the header reading the catalogue for a count behind it.
  knowledge: null,
  // No `/lineage` route exists in the portal — there is no cloud lineage data
  // source yet, which `App.tsx` and the nav both say in their own words. A key
  // that reported a count and then did nothing is worse than no key.
  lineage: null,
  // The chat's row carries `files_node_id`, so the header can name the node it
  // is sharing and open the Files dialog over it in place. Navigating to
  // `/files` instead — which is what this was — left the reader on a drive
  // listing with no roster, no role picker and no way back to the chat.
  share: { kind: "share-dialog" },
  // No settings menu at all in a browser tab. The three editor entries are the
  // editor's — a page has no plugin panel, no background-job view and no editor
  // preferences — and the fourth, signing out, is already where a reader of the
  // portal looks for it: the app shell's own account menu, on every page. A
  // gear on the chat that named the same account and offered that one row was a
  // second door to one place, and the chrome renders no key for an empty list.
  settings: [],
};

/** Read at call time, never at import: a shell installs its host once at boot
 *  and a test installs a fixture. */
export function chatRoutes(): ChatRouteTable {
  return chatHost().kind === "vscode" ? VSCODE : BROWSER;
}

/**
 * Perform a chrome action that needs nothing but the shell: navigate this
 * router, or dispatch an editor command through the host.
 *
 * Two actions are deliberately NOT among them, because a router cannot perform
 * either: signing out ends the session the surface is running inside, and the
 * sharing dialog is held by the surface. Both answer `false` here, so the
 * caller knows it still owes the reader something, rather than a navigation
 * that pretends the key did its job.
 */
export function performChromeAction(
  action: ChromeAction,
  navigate: (path: string) => void,
): boolean {
  if (action.kind === "route") {
    navigate(action.path);
    return true;
  }
  if (action.kind === "command") {
    void chatHost().runCommand({ command: action.command });
    return true;
  }
  return false;
}

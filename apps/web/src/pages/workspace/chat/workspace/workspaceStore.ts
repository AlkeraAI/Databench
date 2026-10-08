// The tabs a reader has open beside a chat, and how they are arranged.
//
// The workspace is a place the reader comes back to, so what is open has to
// survive a refresh, a second laptop and a week away. That makes it server
// state — but it is written at the rate a person clicks, and the thing being
// written is worthless if it arrives late and priceless if it arrives at all.
// So the rules here are about WHEN, not what:
//
//  * a burst of changes is one write, 500 ms after the last of them;
//  * the page going away flushes the pending write in a form that outlives it;
//  * the server refusing the document as too big closes the tabs the reader is
//    least likely to miss and writes once more, never in a loop;
//  * a document arriving from the server while a change of this browser's is
//    still queued or still out on the wire is older than the screen, and is not
//    read in over it.
//
// The tabs live in editor groups, the way an editor splits: each group has its
// own strip and its own tab in front, and the groups are laid out by a tree
// (`editorLayout`). A tab says which group it is in on the tab itself, and the
// arrangement rides beside the tabs as a versioned `layout`. So a build that
// predates groups reads every tab into one strip and, writing the document
// back, keeps each tab's group; a build with groups that finds the layout
// missing rebuilds it from the groups the tabs name.
//
// And the reader's world moves while they are away. A tab names a node, the
// node may have been deleted, replaced by a folder or moved out of the chat
// altogether, and none of that is the reader's fault: on the way back in a tab
// whose node no longer answers is dropped quietly and the shorter document is
// saved. The tab does not open and nothing says "error" — it simply is not
// there, which is what the person expects.
//
// That check belongs to the way back in, and only there. A file that dies while
// its tab is OPEN is the same story told the other way round: the tab stays, and
// says the file is no longer in the chat.

import { create } from "zustand";

import {
  putChatWorkspace,
  type ChatWorkspaceDoc,
  type SaveWorkspaceVars,
  type WorkspaceLayoutWire,
} from "@/api/chats";
import { ApiError } from "@/api/errors";
import { isPathWithin } from "@/lib/paths";

import {
  DEFAULT_GROUP_ID,
  MAX_GROUPS,
  fromWire,
  groupOrder,
  leaf,
  neighbour,
  nudgeAt,
  reconcile,
  removeGroup,
  resizeAt,
  splitAt,
  toWire,
  type LayoutNode,
  type LayoutPath,
  type Side,
} from "./editorLayout";
import { MAX_WORKSPACE_TABS, tabKindFor, type WorkspaceTab } from "./tabKinds";

export type { ChatWorkspaceDoc } from "@/api/chats";

/** The folder browser. It is part of what a workspace IS rather than something
 *  the reader opened, so it is asserted on every hydrate and cannot be closed;
 *  the folder it is showing rides its own params. */
export const FILES_TAB_ID = "files";
export const FILES_TAB_KIND = "files";

/** How long a burst of changes is allowed to settle before it is written. */
export const SAVE_DEBOUNCE_MS = 500;

/** What the reader is told when the server refused the document and tabs had to
 *  go to make it fit. */
export const TRIM_NOTICE = "Some older tabs were closed to keep your workspace small";

/** The server's ceiling on one stored document, spelled here too so the client
 *  can trim toward a target instead of guessing. */
export const MAX_WORKSPACE_STATE_BYTES = 16 * 1024;

/** The version of the stored `layout` this build writes and reads. A layout
 *  stamped with anything else was written by a build that arranges groups some
 *  other way, and is read as absent: the groups the tabs name are laid out in
 *  a row instead. */
export const LAYOUT_VERSION = 1;

/** The refusal that means "close some tabs", as the route spells it. */
const TOO_LARGE = "workspace_state_too_large";

/** A file to open in a tab. `parentId` is only used to point the browser at the
 *  folder the file sits in; it is never stored. */
export interface OpenFileRef {
  nodeId: string;
  name: string;
  path?: string | null;
  parentId?: string | null;
}

/** How a file is opened. */
export interface OpenOptions {
  /** The group to open it in. Unset: where the reader is working (see
   *  `fileTargetGroup`). */
  group?: string;
  /** The view to show it in. Unset: the file's default, and a tab already
   *  showing the file in any view is reused. */
  view?: string;
  /** A preview tab: shown in italics, and replaced by the next file opened the
   *  same way until the reader keeps it. */
  transient?: boolean;
}

/** What the server was able to say about one node a tab names. A node the
 *  caller has not asked about yet is simply absent from the facts — silence is
 *  not evidence that a file is gone. */
export type NodeFact =
  | { present: false }
  | { present: true; kind: string; pathBytes?: string | null };

export interface PruneFacts {
  /** The chat folder's own path. A node that is no longer under it is no longer
   *  the chat's file, however readable it still is. */
  rootPathBytes?: string | null;
  nodes: Record<string, NodeFact>;
}

/** One editor group: which of its tabs is in front. Its tabs are the tabs whose
 *  `group` names it, in strip order. */
export interface EditorGroupState {
  id: string;
  activeTabId: string | null;
}

/** Where a dragged tab is dropped: into a group's strip (at `index` among that
 *  group's tabs, or at the end), or onto a group's edge, which splits it. */
export type TabDrop = { group: string; index?: number } | { group: string; split: Side };

export interface ChatWorkspaceEntry {
  /** Every open tab, in every group. A tab's `group` says which. */
  tabs: WorkspaceTab[];
  /** The groups, in reading order. Never empty. */
  groups: EditorGroupState[];
  layout: LayoutNode;
  /** The group the reader is working in: where a file opens, what the keyboard
   *  acts on. */
  activeGroupId: string;
  /** The tab in front of the active group. */
  activeTabId: string | null;
  /** The server's document has been read at least once. Until it has, nothing
   *  here is worth writing back — an empty workspace would overwrite the
   *  reader's real one. */
  hydrated: boolean;
  /** Tabs whose file changed while the reader was looking at something else. */
  updated: string[];
  /** Tabs whose file has gone while the tab was open. Kept, not closed: the tab
   *  says what happened and offers to restore it. */
  gone: string[];
  /** The node the folder browser should select and scroll to, once. */
  revealId: string | null;
  /** A sentence for the reader about something the store had to do. */
  notice: string | null;
}

interface WorkspaceStore {
  chats: Record<string, ChatWorkspaceEntry>;
  hydrate(chatId: string, doc: ChatWorkspaceDoc | null | undefined): void;
  openFileTab(chatId: string, ref: OpenFileRef, options?: OpenOptions): void;
  /** Open `ref` in the group beside `fromGroup` on the right, making that
   *  group when there is none. */
  openToSide(chatId: string, ref: OpenFileRef, options: { view?: string; fromGroup?: string }): void;
  closeTab(chatId: string, tabId: string): void;
  activate(chatId: string, tabId: string): void;
  /** Put the reader in a group, by id or by its place in reading order. */
  focusGroup(chatId: string, group: string | number): void;
  /** Keep a preview tab. */
  pinTab(chatId: string, tabId: string): void;
  setTabView(chatId: string, tabId: string, view: string): void;
  /** Show the tab (default: the active one) in a new group on `side`, as a
   *  second tab on the same file. */
  splitTab(chatId: string, side: Side, tabId?: string): void;
  moveTab(chatId: string, tabId: string, drop: TabDrop): void;
  resizeSplit(chatId: string, path: LayoutPath, sizes: readonly number[]): void;
  nudgeSplit(chatId: string, path: LayoutPath, index: number, delta: number): void;
  setFilesFolder(chatId: string, folderId: string | null): void;
  reveal(chatId: string, ref: OpenFileRef, options?: { show?: RevealFocus }): void;
  markUpdated(chatId: string, nodeId: string): void;
  markGone(chatId: string, nodeId: string): void;
  markPresent(chatId: string, nodeId: string): void;
  prune(chatId: string, facts: PruneFacts): void;
  clearReveal(chatId: string): void;
  clearNotice(chatId: string): void;
}

type Saver = (vars: SaveWorkspaceVars) => Promise<unknown>;

/** How the store writes. It defaults to the plain transport so the store works
 *  on its own; the page installs the mutation-backed saver so a successful
 *  write also seeds the cache entry the next reader of the layout reads. */
let saver: Saver = putChatWorkspace;

export function setWorkspaceSaver(next: Saver | null): void {
  saver = next ?? putChatWorkspace;
}

const timers = new Map<string, ReturnType<typeof setTimeout>>();
/** Chats whose on-screen workspace differs from what the server was last told. */
const dirty = new Set<string>();
/** For each chat, the nodes its STORED document named that this browser has not
 *  had a usable answer about yet. They are the only tabs `prune` may close:
 *  checking what the document names is what coming BACK to a chat means, and a
 *  file that dies afterwards, while its tab is open, is the tab's own story to
 *  tell. */
const unchecked = new Map<string, Set<string>>();
/** For each chat, how many of this browser's writes are still out. A document
 *  that arrives while one is has nothing to say: it is either that write's own
 *  answer catching up or a read that started before it, and either way it is
 *  older than what the reader has on screen. Counted, not remembered as one
 *  document, because a person clicks faster than a round trip. */
const outstanding = new Map<string, number>();
/** For each chat, a reveal asked for before its stored document arrived. It
 *  cannot be applied to a guess — the first hydrate replaces the strip — so it
 *  waits for the document and is applied on top of it. */
const held = new Map<string, HeldReveal>();

interface HeldReveal {
  ref: OpenFileRef;
  show: RevealFocus;
}

/** Which tab the reader is left looking at once a file has been revealed: the
 *  file itself, or the folder browser with its row selected. */
export type RevealFocus = "file" | "browser";

/** Whether this browser has a change for this chat the server has not confirmed
 *  — waiting on the debounce, or out on the wire. */
function unsettled(chatId: string): boolean {
  return dirty.has(chatId) || (outstanding.get(chatId) ?? 0) > 0;
}

export function emptyEntry(): ChatWorkspaceEntry {
  return {
    tabs: [{ ...filesTab(null), group: DEFAULT_GROUP_ID }],
    groups: [{ id: DEFAULT_GROUP_ID, activeTabId: FILES_TAB_ID }],
    layout: leaf(DEFAULT_GROUP_ID),
    activeGroupId: DEFAULT_GROUP_ID,
    activeTabId: FILES_TAB_ID,
    hydrated: false,
    updated: [],
    gone: [],
    revealId: null,
    notice: null,
  };
}

function filesTab(folderId: string | null): WorkspaceTab {
  return {
    id: FILES_TAB_ID,
    kind: FILES_TAB_KIND,
    name: "Files",
    params: folderId ? { folderId } : {},
  };
}

/** Whether a tab may be closed to make room. The folder browser never can; any
 *  other kind says so through its own registration, so a kind added later
 *  answers this without editing the store. */
function pinned(tab: WorkspaceTab): boolean {
  return tab.id === FILES_TAB_ID || tabKindFor(tab.kind)?.pinned === true;
}

function mint(prefix: string): string {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return `${prefix}-${crypto.randomUUID()}`;
    }
  } catch {
    // A browser without a usable crypto still needs a unique-enough id.
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

const mintTabId = (): string => mint("tab");
const mintGroupId = (): string => mint("g").slice(0, 14);

// -- reading the entry --------------------------------------------------------------

/** The tabs of one group, in strip order. */
export function tabsOf(entry: ChatWorkspaceEntry, groupId: string): WorkspaceTab[] {
  return entry.tabs.filter((tab) => tab.group === groupId);
}

export function groupById(entry: ChatWorkspaceEntry, groupId: string | undefined): EditorGroupState | undefined {
  return entry.groups.find((group) => group.id === groupId);
}

/** The tabs a reader can see right now: the one in front of every group. */
export function visibleTabIds(entry: ChatWorkspaceEntry): Set<string> {
  const ids = new Set<string>();
  for (const group of entry.groups) if (group.activeTabId) ids.add(group.activeTabId);
  return ids;
}

/** Whether `tabId` is in front of its group. */
export function isVisible(entry: ChatWorkspaceEntry | undefined, tabId: string): boolean {
  if (!entry) return false;
  const tab = entry.tabs.find((candidate) => candidate.id === tabId);
  return tab !== undefined && groupById(entry, tab.group)?.activeTabId === tabId;
}

/** Where a file opened from outside a group goes: the group the reader is
 *  working in, unless that group is showing the folder browser and another
 *  group exists, in which case the file opens beside the browser (to its right,
 *  then below it, then the first other group) so the browser stays in view. */
export function fileTargetGroup(entry: ChatWorkspaceEntry): string {
  const active = groupById(entry, entry.activeGroupId);
  if (!active || active.activeTabId !== FILES_TAB_ID || entry.groups.length < 2) {
    return entry.activeGroupId;
  }
  return (
    neighbour(entry.layout, active.id, "right") ??
    neighbour(entry.layout, active.id, "down") ??
    entry.groups.find((group) => group.id !== active.id)?.id ??
    entry.activeGroupId
  );
}

/** Whether a single click in the folder browser should open a preview tab: only
 *  when the file would open in another group than the browser's, so the browser
 *  stays in view. In one group a single click selects and a double click opens. */
export function previewsBesideBrowser(entry: ChatWorkspaceEntry | undefined): boolean {
  if (!entry) return false;
  const browser = entry.tabs.find((tab) => tab.id === FILES_TAB_ID);
  return browser !== undefined && fileTargetGroup(entry) !== browser.group;
}

// -- the stored document -----------------------------------------------------------

/** A tab as it is stored: the fields this build reads, plus anything a newer
 *  writer left on it. A preview flag that is off and a view that is the default
 *  are left out rather than spelled. */
function storedTab(tab: WorkspaceTab): WorkspaceTab {
  const copy: WorkspaceTab = { ...tab };
  if (!copy.transient) delete copy.transient;
  if (copy.view === undefined || copy.view === null) delete copy.view;
  return copy;
}

function layoutWireOf(entry: ChatWorkspaceEntry): WorkspaceLayoutWire {
  const active: Record<string, string> = {};
  for (const group of entry.groups) if (group.activeTabId) active[group.id] = group.activeTabId;
  return { v: LAYOUT_VERSION, root: toWire(entry.layout), active_group: entry.activeGroupId, active };
}

/** The document as the server stores it. Unknown fields a newer build wrote
 *  into a tab ride back out with it. */
function documentOf(entry: ChatWorkspaceEntry): ChatWorkspaceDoc {
  return {
    tabs: entry.tabs.map(storedTab),
    active_tab_id: entry.activeTabId,
    layout: layoutWireOf(entry),
  };
}

function sizeOf(doc: ChatWorkspaceDoc): number {
  return new TextEncoder().encode(JSON.stringify(doc)).length;
}

/** The stored layout, when it is one this build reads. */
function storedLayout(doc: ChatWorkspaceDoc | null | undefined): {
  root: LayoutNode | null;
  activeGroup: string | null;
  active: Record<string, unknown>;
} {
  const raw = doc?.layout as WorkspaceLayoutWire | undefined;
  if (!raw || typeof raw !== "object" || raw.v !== LAYOUT_VERSION) {
    return { root: null, activeGroup: null, active: {} };
  }
  return {
    root: fromWire(raw.root),
    activeGroup: typeof raw.active_group === "string" ? raw.active_group : null,
    active: raw.active && typeof raw.active === "object" ? (raw.active as Record<string, unknown>) : {},
  };
}

/** The entry a stored document describes, before anything this browser knows is
 *  laid over it. Pure, so the same reading decides both what is drawn and
 *  whether a document is news. */
function readDocument(doc: ChatWorkspaceDoc | null | undefined): ChatWorkspaceEntry {
  const stored = Array.isArray(doc?.tabs) ? (doc?.tabs as WorkspaceTab[]) : [];
  // A tab of a kind this build has never heard of is KEPT: dropping it
  // would be a silent loss the reader only notices on the client that could
  // still open it.
  const seen = new Set<string>();
  const valid = stored.filter((tab) => {
    if (!tab || typeof tab.id !== "string" || tab.id === "" || seen.has(tab.id)) return false;
    seen.add(tab.id);
    return true;
  });
  const browser = valid.find((tab) => tab.kind === FILES_TAB_KIND);
  let tabs: WorkspaceTab[] = browser
    ? [{ ...browser, id: FILES_TAB_ID }, ...valid.filter((tab) => tab !== browser)]
    : [filesTab(null), ...valid];
  // A group this build cannot read as a name is no group.
  tabs = tabs.map((tab) => {
    const usable = typeof tab.group === "string" && tab.group !== "";
    const next: WorkspaceTab = usable ? tab : { ...tab };
    if (!usable) delete next.group;
    if (typeof next.transient !== "boolean") delete next.transient;
    if (typeof next.view !== "string") delete next.view;
    return next;
  });

  const layout = storedLayout(doc);
  const used = [...new Set(tabs.map((tab) => tab.group).filter((id): id is string => Boolean(id)))];
  const inLayout = layout.root ? groupOrder(layout.root).filter((id) => used.includes(id)) : [];
  const wanted = [...inLayout, ...used.filter((id) => !inLayout.includes(id))];
  const root = reconcile(layout.root, wanted.length > 0 ? wanted : [DEFAULT_GROUP_ID]);
  const order = groupOrder(root);
  const activeGroup = layout.activeGroup && order.includes(layout.activeGroup) ? layout.activeGroup : (order[0] as string);
  // A tab with no group (one an older build opened) is in the group the
  // reader was working in; a tab whose group did not fit under the ceiling
  // joins the last group that did.
  tabs = tabs.map((tab) => {
    if (!tab.group) return { ...tab, group: activeGroup };
    if (!order.includes(tab.group)) return { ...tab, group: order[order.length - 1] as string };
    return tab;
  });

  const groups: EditorGroupState[] = order.map((id) => {
    const named = layout.active[id];
    return { id, activeTabId: typeof named === "string" ? named : null };
  });
  let activeGroupId = activeGroup;
  const front = typeof doc?.active_tab_id === "string" ? tabs.find((tab) => tab.id === doc.active_tab_id) : undefined;
  if (front?.group) {
    // The tab the document says is in front wins: an older build wrote it
    // without knowing about groups, and it is the last thing the reader did.
    activeGroupId = front.group;
    const group = groups.find((candidate) => candidate.id === front.group);
    if (group) group.activeTabId = front.id;
  } else if (doc?.active_tab_id !== undefined && doc?.active_tab_id !== null) {
    // The tab named is gone: the reader is left on the folder browser.
    const browserTab = tabs.find((tab) => tab.id === FILES_TAB_ID);
    if (browserTab?.group) {
      activeGroupId = browserTab.group;
      const group = groups.find((candidate) => candidate.id === browserTab.group);
      if (group) group.activeTabId = FILES_TAB_ID;
    }
  }

  return settled({
    ...emptyEntry(),
    hydrated: true,
    tabs,
    groups,
    layout: root,
    activeGroupId,
  });
}

/** Whether a node still lives inside the chat's own folder.
 *
 *  Compared on a path SEGMENT boundary, not as a bare prefix: a sibling folder
 *  named after the chat with something appended (`…/Q3.alkerachat-archive`)
 *  starts with the same characters and is a different folder entirely. */
function withinRoot(rootPathBytes: string | null | undefined, pathBytes?: string | null): boolean {
  if (!rootPathBytes) return true;
  if (typeof pathBytes !== "string" || pathBytes === "") return true;
  return isPathWithin(rootPathBytes, pathBytes);
}

/** Whether a node the server answered about is still something a tab can show.
 *  A node the server will not hand back, and a node that turned out to be a
 *  folder, are both "this tab has nothing to show". */
function shows(fact: NodeFact, rootPathBytes: string | null | undefined): boolean {
  if (!fact.present) return false;
  if (fact.kind !== "file") return false;
  return withinRoot(rootPathBytes, fact.pathBytes);
}

/** The first tab a node's file is open in, if the reader has one. The same file
 *  may be open in more than one group; this is the answer to "does the reader
 *  already have this file in front of them", which any of them answers. */
export function tabForNode(
  tabs: readonly WorkspaceTab[],
  nodeId: string,
): WorkspaceTab | undefined {
  return tabs.find((tab) => tab.kind === "file" && tab.node_id === nodeId);
}

/** The nodes the file tabs of a strip name. */
function fileNodes(tabs: WorkspaceTab[]): string[] {
  return tabs
    .filter((tab) => tab.kind === "file" && typeof tab.node_id === "string" && tab.node_id !== "")
    .map((tab) => tab.node_id as string);
}

/** The document as THIS build reads it, spelled so that two of them can be
 *  compared.
 *
 *  A field a newer writer added rides through the store untouched but is no
 *  part of the comparison, and neither is the shape the server gives a document
 *  it has parsed: the answer to a write fills in every default and carries its
 *  own version, so the bytes that come back are never the bytes that went out.
 *  What is being asked is only "is this the workspace this browser just wrote". */
function fingerprint(doc: ChatWorkspaceDoc | null | undefined): string {
  const entry = readDocument(doc);
  const readable = entry.tabs.map((tab) => ({
    id: tab.id,
    kind: tab.kind ?? "",
    node_id: tab.node_id ?? null,
    name: tab.name ?? "",
    path: tab.path ?? null,
    params: tab.params ?? {},
    group: tab.group ?? null,
    view: tab.view ?? null,
    transient: tab.transient === true,
  }));
  return JSON.stringify({ tabs: readable, layout: layoutWireOf(entry), active_tab_id: entry.activeTabId }, byKey);
}

/** Every object spelled with its keys in order, so two equal documents that
 *  were serialized by different writers read the same. */
function byKey(_key: string, value: unknown): unknown {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).sort(([left], [right]) =>
      left < right ? -1 : left > right ? 1 : 0,
    ),
  );
}

/** The entry with its invariants restored: the folder browser first and in a
 *  group, at most the ceiling of tabs, every tab in a group the layout holds,
 *  no empty group beside another, at most one preview tab per group, and a tab
 *  in front of every group that is actually in it. */
function settled(entry: ChatWorkspaceEntry): ChatWorkspaceEntry {
  let layout = entry.layout;
  let order = groupOrder(layout);
  const fallback = order.includes(entry.activeGroupId) ? entry.activeGroupId : (order[0] as string);

  const browser = entry.tabs.find((tab) => tab.id === FILES_TAB_ID) ?? filesTab(null);
  const rest = entry.tabs.filter((tab) => tab.id !== FILES_TAB_ID);
  let tabs = [browser, ...rest].map((tab) =>
    tab.group && order.includes(tab.group) ? tab : { ...tab, group: fallback },
  );

  if (tabs.length > MAX_WORKSPACE_TABS) {
    tabs = trimTo(tabs, frontOf(entry), MAX_WORKSPACE_TABS);
  }

  // One preview tab per group: the newest keeps the italics.
  const previewed = new Set<string>();
  for (let index = tabs.length - 1; index >= 0; index -= 1) {
    const tab = tabs[index] as WorkspaceTab;
    if (!tab.transient) continue;
    if (previewed.has(tab.group as string)) tabs[index] = { ...tab, transient: false };
    else previewed.add(tab.group as string);
  }

  // A group with nothing in it is not drawn beside one that has something.
  for (const id of order) {
    if (order.length <= 1) break;
    if (tabs.some((tab) => tab.group === id)) continue;
    const next = removeGroup(layout, id);
    if (next !== null) {
      layout = next;
      order = groupOrder(layout);
    }
  }

  const groups = order.map((id) => {
    const before = entry.groups.find((group) => group.id === id);
    const mine = tabs.filter((tab) => tab.group === id);
    const front = mine.some((tab) => tab.id === before?.activeTabId) ? (before?.activeTabId ?? null) : (mine[0]?.id ?? null);
    return { id, activeTabId: front };
  });
  const activeGroupId = order.includes(entry.activeGroupId) ? entry.activeGroupId : (order[0] as string);
  const activeTabId = groups.find((group) => group.id === activeGroupId)?.activeTabId ?? null;
  const ids = new Set(tabs.map((tab) => tab.id));
  const visible = new Set(groups.map((group) => group.activeTabId).filter(Boolean));
  return {
    ...entry,
    tabs,
    groups,
    layout,
    activeGroupId,
    activeTabId,
    updated: entry.updated.filter((id) => ids.has(id) && !visible.has(id)),
    gone: entry.gone.filter((id) => ids.has(id)),
  };
}

/** The tabs in front of their groups, which the ceiling never closes. */
function frontOf(entry: ChatWorkspaceEntry): Set<string> {
  const front = visibleTabIds(entry);
  if (entry.activeTabId) front.add(entry.activeTabId);
  return front;
}

/** Close the oldest tabs the reader is not in, until the strip is `limit` long.
 *  Oldest means earliest in the strip, which is the order they were opened. */
function trimTo(tabs: WorkspaceTab[], keep: ReadonlySet<string>, limit: number): WorkspaceTab[] {
  const kept = [...tabs];
  for (let index = 0; kept.length > limit && index < kept.length; ) {
    const tab = kept[index];
    if (tab && !pinned(tab) && !keep.has(tab.id)) kept.splice(index, 1);
    else index += 1;
  }
  return kept;
}

/** The entry with `tabId` in front of its group, and that group the active one. */
function bringForward(entry: ChatWorkspaceEntry, tabId: string): ChatWorkspaceEntry {
  const tab = entry.tabs.find((candidate) => candidate.id === tabId);
  if (!tab?.group) return entry;
  return {
    ...entry,
    activeGroupId: tab.group,
    groups: entry.groups.map((group) => (group.id === tab.group ? { ...group, activeTabId: tabId } : group)),
    updated: entry.updated.filter((id) => id !== tabId),
  };
}

/** The flat list with `tab` placed at `index` among the tabs of its group. The
 *  flat order is the strip order inside each group, so a tab is placed by
 *  putting it just before the group-mate it now precedes. */
function placeAt(tabs: WorkspaceTab[], tab: WorkspaceTab, index: number | undefined): WorkspaceTab[] {
  const others = tabs.filter((candidate) => candidate.id !== tab.id);
  const mates = others.filter((candidate) => candidate.group === tab.group);
  const before = index === undefined ? undefined : mates[Math.max(0, index)];
  if (before === undefined) return [...others, tab];
  const at = others.indexOf(before);
  return [...others.slice(0, at), tab, ...others.slice(at)];
}

/** The neighbour a group's front passes to when its front tab leaves: the tab to
 *  its right, then to its left. */
function heirOf(entry: ChatWorkspaceEntry, tabId: string): string | null {
  const tab = entry.tabs.find((candidate) => candidate.id === tabId);
  if (!tab) return null;
  const mates = tabsOf(entry, tab.group as string);
  const index = mates.findIndex((candidate) => candidate.id === tabId);
  return (mates[index + 1] ?? mates[index - 1])?.id ?? null;
}

/** The group the reader lands in when `groupId` goes: the one that takes its
 *  space, which is the one before it in reading order, else the one after. */
function groupHeir(entry: ChatWorkspaceEntry, groupId: string): string | null {
  const order = groupOrder(entry.layout);
  const index = order.indexOf(groupId);
  return order[index - 1] ?? order[index + 1] ?? null;
}

/** The entry without `tabId`, its group's front passed on and, when the group is
 *  left empty beside others, the reader moved to the group that absorbs it. */
function without(entry: ChatWorkspaceEntry, tabId: string): ChatWorkspaceEntry {
  const tab = entry.tabs.find((candidate) => candidate.id === tabId);
  if (!tab) return entry;
  const groupId = tab.group as string;
  const heir = heirOf(entry, tabId);
  const tabs = entry.tabs.filter((candidate) => candidate.id !== tabId);
  const groups = entry.groups.map((group) =>
    group.id === groupId && group.activeTabId === tabId ? { ...group, activeTabId: heir } : group,
  );
  let activeGroupId = entry.activeGroupId;
  if (heir === null && activeGroupId === groupId && entry.groups.length > 1) {
    activeGroupId = groupHeir(entry, groupId) ?? activeGroupId;
  }
  return { ...entry, tabs, groups, activeGroupId };
}

export const useWorkspaceStore = create<WorkspaceStore>((set, get) => {
  /** Apply a change to one chat and, unless it came from the server, queue the
   *  write. `changed` is what decides: a read that found nothing to fix must
   *  not write the document back. */
  function update(
    chatId: string,
    change: (entry: ChatWorkspaceEntry) => ChatWorkspaceEntry,
    options: { save?: boolean } = {},
  ): void {
    const current = get().chats[chatId] ?? emptyEntry();
    const changed = change(current);
    if (changed === current) return;
    const next = settled(changed);
    set((state) => ({ chats: { ...state.chats, [chatId]: next } }));
    if (options.save !== false) scheduleSave(chatId);
  }

  /** The entry with `ref` open in `groupId` and in front of it. */
  function opened(entry: ChatWorkspaceEntry, ref: OpenFileRef, groupId: string, options: OpenOptions): ChatWorkspaceEntry {
    const mates = tabsOf(entry, groupId);
    const open = mates.find(
      (tab) =>
        tab.kind === "file" &&
        tab.node_id === ref.nodeId &&
        (options.view === undefined || (tab.view ?? null) === options.view),
    );
    if (open) {
      // Opening a file the group already shows brings it forward; opening it
      // for keeps keeps a preview tab of it.
      const kept = options.transient ? open : { ...open, transient: false };
      const tabs = entry.tabs.map((tab) => (tab.id === open.id ? kept : tab));
      return bringForward({ ...entry, tabs }, open.id);
    }
    const tab: WorkspaceTab = {
      id: mintTabId(),
      kind: "file",
      node_id: ref.nodeId,
      name: ref.name,
      path: ref.path ?? null,
      group: groupId,
      ...(options.view !== undefined ? { view: options.view } : {}),
      ...(options.transient ? { transient: true } : {}),
    };
    // A preview tab takes the place of the group's last one.
    const replaced = options.transient ? mates.find((candidate) => candidate.transient) : undefined;
    let tabs: WorkspaceTab[];
    if (replaced) {
      tabs = entry.tabs.map((candidate) => (candidate.id === replaced.id ? tab : candidate));
    } else {
      // Make room before the tab lands, so the tab just opened is never the
      // one the ceiling closes.
      tabs = [...trimTo(entry.tabs, frontOf(entry), MAX_WORKSPACE_TABS - 1), tab];
    }
    const gone = replaced ? entry.gone.filter((id) => id !== replaced.id) : entry.gone;
    return bringForward({ ...entry, tabs, gone }, tab.id);
  }

  return {
    chats: {},

    hydrate(chatId, doc) {
      const current = get().chats[chatId];
      // What the store already holds is what decides. A document that arrives
      // while a change of this browser's is unsettled is older than the screen
      // — with two writes in flight the first one's answer carries the document
      // from BEFORE the second, and reading it in would undo the tab opened
      // since. A document equal to what is on screen is not news either.
      if (
        current?.hydrated &&
        (unsettled(chatId) || fingerprint(doc) === fingerprint(documentOf(current)))
      ) {
        return;
      }
      const read = readDocument(doc);
      const entry: ChatWorkspaceEntry = {
        ...read,
        // A layout can also arrive from the reader's other laptop. It says
        // which tabs are open and nothing else: what this browser noticed about
        // the files in them is still true, and still this browser's to say.
        updated: current?.updated ?? [],
        gone: current?.gone ?? [],
        revealId: current?.revealId ?? null,
        notice: current?.notice ?? null,
        hydrated: true,
      };
      // Reading the server's own document is not a change to write back.
      update(chatId, () => entry, { save: false });
      // The tabs it names are the ones still to be checked against the drive.
      unchecked.set(chatId, new Set(fileNodes(get().chats[chatId]?.tabs ?? [])));
      // A file the reader asked for before any of this arrived belongs on top
      // of the document, not under it.
      const waiting = held.get(chatId);
      if (waiting) {
        held.delete(chatId);
        get().reveal(chatId, waiting.ref, { show: waiting.show });
      }
    },

    openFileTab(chatId, ref, options = {}) {
      update(chatId, (entry) => {
        const groupId = options.group && groupById(entry, options.group) ? options.group : fileTargetGroup(entry);
        return opened(entry, ref, groupId, options);
      });
    },

    openToSide(chatId, ref, options) {
      update(chatId, (entry) => {
        const from = options.fromGroup && groupById(entry, options.fromGroup) ? options.fromGroup : entry.activeGroupId;
        const beside = neighbour(entry.layout, from, "right");
        if (beside !== null) return opened(entry, ref, beside, { view: options.view });
        if (entry.groups.length >= MAX_GROUPS) {
          // No room for another group: the group furthest right takes it.
          const order = groupOrder(entry.layout);
          return opened(entry, ref, order[order.length - 1] as string, { view: options.view });
        }
        const id = mintGroupId();
        const split = {
          ...entry,
          layout: splitAt(entry.layout, from, id, "right"),
          groups: [...entry.groups, { id, activeTabId: null }],
        };
        return opened(split, ref, id, { view: options.view });
      });
    },

    closeTab(chatId, tabId) {
      update(chatId, (entry) => {
        const tab = entry.tabs.find((candidate) => candidate.id === tabId);
        if (!tab || pinned(tab)) return entry;
        // Closing the tab in front leaves the reader on its right-hand
        // neighbour, the way an editor does; closing a group's last tab closes
        // the group, and the reader lands in the one that takes its space.
        return without(entry, tabId);
      });
    },

    activate(chatId, tabId) {
      update(chatId, (entry) =>
        entry.tabs.some((tab) => tab.id === tabId) ? bringForward(entry, tabId) : entry,
      );
    },

    focusGroup(chatId, group) {
      update(chatId, (entry) => {
        const order = groupOrder(entry.layout);
        const id = typeof group === "number" ? order[group] : group;
        if (id === undefined || !order.includes(id) || id === entry.activeGroupId) return entry;
        return { ...entry, activeGroupId: id };
      });
    },

    pinTab(chatId, tabId) {
      update(chatId, (entry) => {
        const tab = entry.tabs.find((candidate) => candidate.id === tabId);
        if (!tab?.transient) return entry;
        return { ...entry, tabs: entry.tabs.map((candidate) => (candidate.id === tabId ? { ...candidate, transient: false } : candidate)) };
      });
    },

    setTabView(chatId, tabId, view) {
      update(chatId, (entry) => {
        const tab = entry.tabs.find((candidate) => candidate.id === tabId);
        if (!tab || tab.view === view) return entry;
        return { ...entry, tabs: entry.tabs.map((candidate) => (candidate.id === tabId ? { ...candidate, view } : candidate)) };
      });
    },

    splitTab(chatId, side, tabId) {
      update(chatId, (entry) => {
        const tab = entry.tabs.find((candidate) => candidate.id === (tabId ?? entry.activeTabId));
        // The folder browser is one place, not a file: it is moved, never copied.
        if (!tab || tab.kind !== "file" || entry.groups.length >= MAX_GROUPS) return entry;
        if (entry.tabs.length >= MAX_WORKSPACE_TABS) return entry;
        const id = mintGroupId();
        const copy: WorkspaceTab = { ...storedTab(tab), id: mintTabId(), group: id };
        delete copy.transient;
        const tabs = entry.tabs.map((candidate) => (candidate.id === tab.id ? { ...candidate, transient: false } : candidate));
        return bringForward(
          {
            ...entry,
            tabs: [...tabs, copy],
            layout: splitAt(entry.layout, tab.group as string, id, side),
            groups: [...entry.groups, { id, activeTabId: copy.id }],
          },
          copy.id,
        );
      });
    },

    moveTab(chatId, tabId, drop) {
      update(chatId, (entry) => {
        const tab = entry.tabs.find((candidate) => candidate.id === tabId);
        if (!tab || !groupById(entry, drop.group)) return entry;
        const source = tab.group as string;
        const alone = tabsOf(entry, source).length === 1;

        if ("split" in drop) {
          // Splitting a group by its only tab would leave the same one group.
          if (alone && drop.group === source) return entry;
          const groups = entry.groups.length + 1 - (alone ? 1 : 0);
          if (groups > MAX_GROUPS) return entry;
          const id = mintGroupId();
          const moved: WorkspaceTab = { ...tab, group: id, transient: false };
          const left = without(entry, tabId);
          let layout = splitAt(left.layout, drop.group, id, drop.split);
          if (alone) layout = removeGroup(layout, source) ?? layout;
          return bringForward(
            { ...left, tabs: [...left.tabs, moved], layout, groups: [...left.groups, { id, activeTabId: moved.id }] },
            moved.id,
          );
        }

        if (drop.group === source) {
          return bringForward({ ...entry, tabs: placeAt(entry.tabs, tab, drop.index) }, tabId);
        }
        // A group that already shows this file in this view keeps its own tab.
        const twin = tabsOf(entry, drop.group).find(
          (candidate) => tab.kind === "file" && candidate.node_id === tab.node_id && (candidate.view ?? null) === (tab.view ?? null),
        );
        if (twin && tab.id !== FILES_TAB_ID) return bringForward(without(entry, tabId), twin.id);
        const left = without(entry, tabId);
        const moved: WorkspaceTab = { ...tab, group: drop.group, transient: false };
        return bringForward({ ...left, tabs: placeAt(left.tabs, moved, drop.index) }, moved.id);
      });
    },

    resizeSplit(chatId, path, sizes) {
      update(chatId, (entry) => ({ ...entry, layout: resizeAt(entry.layout, path, sizes) }));
    },

    nudgeSplit(chatId, path, index, delta) {
      update(chatId, (entry) => ({ ...entry, layout: nudgeAt(entry.layout, path, index, delta) }));
    },

    setFilesFolder(chatId, folderId) {
      update(chatId, (entry) => ({
        ...entry,
        tabs: entry.tabs.map((tab) =>
          tab.id === FILES_TAB_ID
            ? { ...tab, params: folderId ? { ...tab.params, folderId } : {} }
            : tab,
        ),
      }));
    },

    reveal(chatId, ref, options = {}) {
      const show = options.show ?? "file";
      // Before the stored document lands the strip is a guess: a tab opened on
      // it is never written (a guess must not be saved over the reader's real
      // workspace) and the first hydrate replaces it. So the request waits.
      if (!get().chats[chatId]?.hydrated) {
        held.set(chatId, { ref, show });
        return;
      }
      const store = get();
      store.setFilesFolder(chatId, ref.parentId ?? null);
      set((state) => {
        const entry = state.chats[chatId];
        return entry ? { chats: { ...state.chats, [chatId]: { ...entry, revealId: ref.nodeId } } } : state;
      });
      store.openFileTab(chatId, ref);
      if (show === "browser") store.activate(chatId, FILES_TAB_ID);
    },

    markUpdated(chatId, nodeId) {
      update(
        chatId,
        (entry) => {
          // A tab in front of its group is being read right now; there is
          // nothing to announce about a change the reader is watching happen.
          const visible = visibleTabIds(entry);
          const marked = entry.tabs
            .filter((tab) => tab.node_id === nodeId && !visible.has(tab.id) && !entry.updated.includes(tab.id))
            .map((tab) => tab.id);
          if (marked.length === 0) return entry;
          return { ...entry, updated: [...entry.updated, ...marked] };
        },
        // A marker is what this browser noticed, not part of the layout.
        { save: false },
      );
    },

    markGone(chatId, nodeId) {
      update(
        chatId,
        (entry) => {
          const marked = entry.tabs
            .filter((tab) => tab.node_id === nodeId && !entry.gone.includes(tab.id))
            .map((tab) => tab.id);
          if (marked.length === 0) return entry;
          return { ...entry, gone: [...entry.gone, ...marked] };
        },
        { save: false },
      );
    },

    markPresent(chatId, nodeId) {
      // The other half of the pair: the node answers for itself again — it was
      // put back, or the drive was only briefly unreachable — so the tab stops
      // saying the file is not there.
      update(
        chatId,
        (entry) => {
          const ids = new Set(entry.tabs.filter((tab) => tab.node_id === nodeId).map((tab) => tab.id));
          if (!entry.gone.some((id) => ids.has(id))) return entry;
          return { ...entry, gone: entry.gone.filter((id) => !ids.has(id)) };
        },
        { save: false },
      );
    },

    prune(chatId, facts) {
      const entry = get().chats[chatId];
      const pending = unchecked.get(chatId);
      if (!entry || !pending || pending.size === 0) return;
      const closing = new Set<string>();
      for (const nodeId of [...pending]) {
        const fact = facts.nodes[nodeId];
        if (fact === undefined) continue;
        if (!shows(fact, facts.rootPathBytes)) {
          pending.delete(nodeId);
          closing.add(nodeId);
        } else if (facts.rootPathBytes) {
          // Answered for itself, and measured against the chat's own folder —
          // which a node that answered before that folder's path arrived has
          // not been. From here the file is the open tab's business.
          pending.delete(nodeId);
        }
      }
      if (closing.size === 0) return;
      update(chatId, (current) => {
        const dropped = (tab: WorkspaceTab): boolean =>
          tab.kind === "file" && typeof tab.node_id === "string" && closing.has(tab.node_id);
        const ids = new Set(current.tabs.filter(dropped).map((tab) => tab.id));
        // A tab that never opened hands its group to the group's first tab
        // (the folder browser, where it is the browser's group) rather than to
        // a neighbour the reader was not looking at either.
        return {
          ...current,
          tabs: current.tabs.filter((tab) => !ids.has(tab.id)),
          groups: current.groups.map((group) =>
            group.activeTabId && ids.has(group.activeTabId) ? { ...group, activeTabId: null } : group,
          ),
        };
      });
    },

    clearReveal(chatId) {
      update(chatId, (entry) => (entry.revealId === null ? entry : { ...entry, revealId: null }), { save: false });
    },

    clearNotice(chatId) {
      update(chatId, (entry) => (entry.notice === null ? entry : { ...entry, notice: null }), { save: false });
    },
  };
});

/** The store's own view of one chat, for callers outside React. */
export function workspaceOf(chatId: string): ChatWorkspaceEntry | undefined {
  return useWorkspaceStore.getState().chats[chatId];
}

let flushHookInstalled = false;

/** Write the pending change out while the page still exists.
 *
 *  A tab being hidden is the last moment a browser reliably runs anything, and
 *  a normal request started there is cancelled with the page. `keepalive` is
 *  what makes the write survive it. */
function ensureFlushHook(): void {
  if (flushHookInstalled || typeof document === "undefined") return;
  flushHookInstalled = true;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden") return;
    for (const chatId of [...dirty]) void flushWorkspace(chatId, { keepalive: true });
  });
}

function scheduleSave(chatId: string): void {
  const entry = useWorkspaceStore.getState().chats[chatId];
  // Until the server's document has been read, what is on screen is a guess,
  // and writing a guess would overwrite the reader's real workspace.
  if (!entry?.hydrated) return;
  ensureFlushHook();
  dirty.add(chatId);
  const pending = timers.get(chatId);
  if (pending) clearTimeout(pending);
  timers.set(
    chatId,
    setTimeout(() => {
      timers.delete(chatId);
      void flushWorkspace(chatId);
    }, SAVE_DEBOUNCE_MS),
  );
}

function isTooLarge(error: unknown): boolean {
  return error instanceof ApiError && error.status === 422 && error.code === TOO_LARGE;
}

/** Write this chat's workspace now, if there is anything to write.
 *
 *  A refusal of "too big" is answered once: the oldest tabs the reader is not
 *  in are closed until the document is under the server's ceiling — at least
 *  one, since the server has already said this build's estimate was optimistic
 *  — and the shorter document is written. A second refusal is left alone rather
 *  than closing tabs until the strip is empty. */
export async function flushWorkspace(
  chatId: string,
  options: { keepalive?: boolean } = {},
): Promise<void> {
  const pending = timers.get(chatId);
  if (pending) {
    clearTimeout(pending);
    timers.delete(chatId);
  }
  if (!dirty.has(chatId)) return;
  const entry = useWorkspaceStore.getState().chats[chatId];
  if (!entry?.hydrated) return;
  dirty.delete(chatId);

  try {
    await send(chatId, documentOf(entry), options.keepalive);
    return;
  } catch (error) {
    if (!isTooLarge(error)) return;
  }

  // The chat can be deleted while the refusal is in flight. Closing tabs and
  // writing again would then be a PUT to a chat the server no longer has.
  if (!useWorkspaceStore.getState().chats[chatId]) return;
  const trimmed = trimForSize(entry);
  if (!trimmed) return;
  useWorkspaceStore.setState((state) =>
    state.chats[chatId] ? { chats: { ...state.chats, [chatId]: trimmed } } : state,
  );
  try {
    await send(chatId, documentOf(trimmed), options.keepalive);
  } catch {
    // The reader's tabs are already the shorter set; another write would only
    // be refused the same way.
  }
}

/** Write one document, counted as outstanding for as long as it is out. */
async function send(chatId: string, doc: ChatWorkspaceDoc, keepalive?: boolean): Promise<void> {
  outstanding.set(chatId, (outstanding.get(chatId) ?? 0) + 1);
  try {
    await saver({ chatId, state: doc, keepalive });
  } finally {
    const left = (outstanding.get(chatId) ?? 1) - 1;
    if (left > 0) outstanding.set(chatId, left);
    else outstanding.delete(chatId);
  }
}

/** The entry with enough of the oldest closable tabs gone for the document to
 *  fit, or `null` when there was nothing left to close. */
function trimForSize(entry: ChatWorkspaceEntry): ChatWorkspaceEntry | null {
  let current = entry;
  let closed = 0;
  const keep = frontOf(entry);
  for (;;) {
    const victim = current.tabs.find((tab) => !pinned(tab) && !keep.has(tab.id));
    if (!victim) break;
    current = settled(without(current, victim.id));
    closed += 1;
    if (sizeOf(documentOf(current)) <= MAX_WORKSPACE_STATE_BYTES) break;
  }
  if (closed === 0) return null;
  return settled({ ...current, notice: TRIM_NOTICE });
}

/** Drop everything held for a chat that is gone. No write follows: there is
 *  nothing left on the server to write it to. */
export function forgetChatPane(chatId: string): void {
  const pending = timers.get(chatId);
  if (pending) clearTimeout(pending);
  timers.delete(chatId);
  dirty.delete(chatId);
  unchecked.delete(chatId);
  outstanding.delete(chatId);
  held.delete(chatId);
  useWorkspaceStore.setState((state) => {
    if (!(chatId in state.chats)) return state;
    const chats = { ...state.chats };
    delete chats[chatId];
    return { chats };
  });
}

// The cloud chat reads the portal's own chrome needs: the rail that lists
// chats, and the machine the chat is bound to.
//
// The transcript does NOT come through here — it rides the doc-sync socket into
// the chat's data source, which is what makes a token appear as it is written
// rather than on a refetch. These are the reads that stale when a
// `chat.updated` or `compute_machine.changed` frame lands, which is exactly
// what `EVENT_KEYS` invalidates.

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";
import type { components } from "@alkera/sdk";

import { activityKeys, chatKeys } from "@/pages/workspace/chat/chatKeys";
import { MARK_READ_DEBOUNCE_MS, MACHINE_POLL_MS as WEB_MACHINE_POLL_MS } from "@/lib/limits";

import { apiBaseUrl, apiFetch, failedResponse } from "./client";
import { ApiError, refusalSentence } from "./errors";
import { keys } from "./keys";
import type { MachineUnavailableRead } from "./machines";
import {
  createChat,
  currentMachine,
  deleteChat,
  getChat,
  listAllChats,
  markChatRead,
  markChatUnread,
  markWorkspaceRead,
  wakeChat,
  wakeWorkspace,
  type ChatReadStateRead,
  type ChatSessionList,
  type ChatWakeRead,
  type ChatSessionRead,
  type MachineStateRead,
} from "./cloudChat/transport";

export type { ChatSessionRead, MachineStateRead } from "./cloudChat/transport";

export function useChats() {
  return useQuery<ChatSessionList>({
    queryKey: keys.chats.all,
    // Every chat, not the first page of them: the rail and the dashboard read
    // off this and an older chat must not read as a deleted one.
    queryFn: () => listAllChats(),
  });
}

/** How often the machine's state is re-read even when no event says to.
 *
 *  `compute_machine.changed` invalidates these keys, and that is the fast path
 *  — but the frame travels over the same connection a dead box may have taken
 *  down with it, and the transition it announces (a machine that went quiet) is
 *  emitted by a periodic sweep rather than by the machine itself. A poll is the
 *  floor under both: whatever else fails, the banner is at most this stale.
 *
 *  Sized against the drill, not by feel: the reachability window plus this
 *  interval is what the reader waits to be told a box has died, and the budget
 *  for that is a minute.
 */
export const MACHINE_POLL_MS = WEB_MACHINE_POLL_MS;

/** The chat row, which carries the binding AND the one thing only the machine
 *  can say — that it is running but was refused this chat's transcript. Polled
 *  on the same floor as the machine for the same reason. */
export function useChat(chatId: string | undefined) {
  return useQuery<ChatSessionRead>({
    queryKey: keys.chats.one(chatId),
    queryFn: () => getChat(chatId as string),
    enabled: Boolean(chatId),
    // A chat the server has no record of has no machine, so there is no state
    // for the poll to catch up with: the same refusal would be asked for every
    // fifteen seconds, for as long as the tab is open, behind a page that has
    // already stopped showing the chat. Every other failure keeps its poll — a
    // 500 or an offline browser IS a state that changes.
    refetchInterval: (query) =>
      query.state.error instanceof ApiError && query.state.error.status === 404
        ? false
        : MACHINE_POLL_MS,
  });
}

/** What opening a chat asks of the server, and what it said no with. */
export interface WakeOnOpen {
  /** Ask again (a notebook tab opening, a Run). Does nothing for a reader the
   *  server said may not send, or while the chat already reads awake. */
  wake: () => void;
  /** The server's sentence when it would not start the machine (the org is
   *  out of credit, or at its limit), else null. */
  refusal: string | null;
  /** Set when the server held the wake because the workspace's machine is
   *  gone: what was lost and where the reader may wake it instead. */
  unavailable: MachineUnavailableRead | null;
}

/** The one wake an open asks for. `id` is what was opened, or undefined while
 *  nothing may be asked for it. Asked once when it opens and again when the
 *  window comes back into focus, never on a render, and never while `awake`
 *  says it is already up. `awake` is read when the ask is made and never
 *  causes one: a chat that goes back to sleep under an open page stays asleep
 *  until the reader comes back to it.
 *
 *  Nothing is invalidated here: a wake that changed the chat or its machine is
 *  announced on the event stream, which refreshes the reads that show it. */
function useOpenWake(
  id: string | undefined,
  awake: boolean,
  ask: (id: string) => Promise<ChatWakeRead | undefined>,
): WakeOnOpen {
  const { mutate, error, data } = useMutation({ mutationFn: ask, meta: { invalidates: "none" } });
  const up = useRef(awake);
  up.current = awake;
  const wake = useCallback(() => {
    if (id && !up.current) mutate(id);
  }, [id, mutate]);
  useEffect(() => {
    wake();
    window.addEventListener("focus", wake);
    return () => window.removeEventListener("focus", wake);
  }, [wake]);
  const refused = error instanceof ApiError && (error.status === 402 || error.status === 429);
  return {
    wake,
    refusal: refused ? refusalSentence(error) : null,
    unavailable:
      data?.outcome === "machine_unavailable" ? (data.machine_unavailable ?? null) : null,
  };
}

/** The session states under which a chat is already up, so there is nothing
 *  to wake. */
const UP: ReadonlySet<string> = new Set(["awake", "working"]);

/** Whether a chat whose session reads `state` is already up. */
export function sessionIsUp(state: string | null | undefined): boolean {
  return UP.has(state ?? "");
}

/** Wakes a sleeping chat when it is opened, so its workspace runs before
 *  anyone sends a message. `canSend` and `sessionState` are the server's own
 *  answers on the chat row: nothing is asked until `canSend` is `true`, or
 *  while the chat reads awake. The server throttles the wake per chat, so
 *  several tabs ask once. */
export function useWakeOnOpen(
  chatId: string | undefined,
  canSend: boolean | undefined,
  sessionState: string | null | undefined,
): WakeOnOpen {
  return useOpenWake(canSend === true ? chatId : undefined, sessionIsUp(sessionState), wakeChat);
}

/** The same for a workspace opened with no chat. The server picks the chat
 *  (the most recently active one this reader may send in) and decides whether
 *  there is one; `awake` is whether the workspace already reads as up. */
export function useWakeWorkspaceOnOpen(
  workspaceId: string | undefined,
  awake: boolean,
): WakeOnOpen {
  return useOpenWake(workspaceId, awake, wakeWorkspace);
}

/** Lays the server's answer for one chat's read state over the listed row, so
 *  the rail follows a mark the moment the server took it. */
function seedReadState(qc: QueryClient, state: ChatReadStateRead): void {
  qc.setQueryData<ChatSessionList>(keys.chats.all, (list) =>
    list
      ? {
          ...list,
          items: list.items.map((row) =>
            row.id === state.chat_id ? { ...row, unread: state.unread, needs_you: state.needs_you } : row,
          ),
        }
      : list,
  );
}

/** Marks the open chat read as far as the server has written it, once the
 *  sequence stops moving for a moment. What streams into an open chat is
 *  read, so this follows `lastSeq` as long as the page is visible. `unread`
 *  is the server's word on the chat's listed row: a chat that opens already
 *  read costs no request until something new is written, and nothing is
 *  asked before the list has said. Only the caller's
 *  own mark moves; the count on the workspace row follows on the workspace
 *  list's re-read. */
export function useMarkReadOnView(
  chatId: string | undefined,
  lastSeq: number | undefined,
  unread: boolean | undefined,
): void {
  const qc = useQueryClient();
  const { mutate } = useMutation({
    mutationFn: (vars: { chatId: string; seq: number }) => markChatRead(vars.chatId, vars.seq),
    onSuccess: (state) => seedReadState(qc, state),
    meta: { invalidates: [keys.workspaces.all] },
  });
  // How far this page has told the server, per chat. A chat seen for the
  // first time starts at its opening sequence when it opened read.
  const told = useRef<{ chatId: string; seq: number } | null>(null);
  useEffect(() => {
    if (!chatId || lastSeq === undefined || unread === undefined) return;
    if (told.current?.chatId !== chatId) {
      told.current = { chatId, seq: unread ? -1 : lastSeq };
    }
    const mark = (): void => {
      if (document.visibilityState !== "visible") return;
      const held = told.current;
      if (held && held.chatId === chatId && held.seq >= lastSeq) return;
      told.current = { chatId, seq: lastSeq };
      mutate({ chatId, seq: lastSeq });
    };
    const timer = window.setTimeout(mark, MARK_READ_DEBOUNCE_MS);
    document.addEventListener("visibilitychange", mark);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", mark);
    };
  }, [chatId, lastSeq, unread, mutate]);
}

/** "Mark as unread" on a chat, for the caller alone. */
export function useMarkChatUnread() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (chatId: string) => markChatUnread(chatId),
    onSuccess: (state) => seedReadState(qc, state),
    meta: { invalidates: [keys.workspaces.all] },
  });
}

/** "Mark all as read" on a workspace, for the caller alone. */
export function useMarkWorkspaceRead() {
  return useMutation({
    mutationFn: (workspaceId: string) => markWorkspaceRead(workspaceId),
    meta: { invalidates: [keys.workspaces.all, keys.chats.all] },
  });
}

/** The org's workspace machine, or with `workspaceId` the machine a chat of
 *  that workspace runs on. Everything a chat can say about whether it is
 *  reachable comes from here, so a send never hangs with nothing on screen. */
export function useCurrentMachine(
  workspaceId?: string | null,
  { enabled = true }: { enabled?: boolean } = {},
) {
  return useQuery<MachineStateRead>({
    queryKey: workspaceId ? keys.machines.currentFor(workspaceId) : keys.machines.current,
    queryFn: () => currentMachine(workspaceId ?? undefined),
    enabled,
    refetchInterval: MACHINE_POLL_MS,
    // A reader who left the tab open on a chat comes back to the truth, not to
    // whatever the machine was doing when they looked away.
    refetchOnWindowFocus: true,
  });
}

/** Opens a chat.
 *
 *  `sourceNodeId` names a `.alkerareport` / `.alkeraquery` folder to start the
 *  chat FROM: the box reads that folder's spec and README into the chat's first
 *  turn so the agent asks the context's questions before it re-runs anything.
 *  It is a reference and never a grant — the server decides READ on the node
 *  for this caller, and a node they cannot read is the same opaque 404 any
 *  other read of it would give. */
export function useCreateChat() {
  return useMutation({
    mutationFn: (vars: { title: string | null; sourceNodeId?: string; workspaceId?: string }) =>
      createChat(vars.title, {
        ...(vars.sourceNodeId ? { sourceNodeId: vars.sourceNodeId } : {}),
        ...(vars.workspaceId ? { workspaceId: vars.workspaceId } : {}),
      }),
    // A chat started in a workspace changes that workspace's count and state.
    meta: { invalidates: [keys.chats.all, keys.workspaces.all] },
  });
}

/** Deletes a chat. The rail re-reads on success; the chat's own entries are
 *  dropped rather than refreshed, because there is nothing left to read.
 *
 *  Sharing the list's prefix is exactly the problem: `["chats"]` also covers
 *  the chat's own row, its messages and its attachments, so the policy's
 *  awaited invalidation would re-ask for all three — each one now a 404 that
 *  climbs the retry ladder before it settles, holding the caller on a delete
 *  the server finished seconds earlier. Dropping them is also what the reader
 *  should see: no cached transcript outlives the chat it belongs to.
 *
 *  This runs inside the mutation rather than in `onSuccess` because the cache
 *  policy's invalidation runs FIRST among the success callbacks; anything that
 *  must happen before the refetch has to happen before the mutation resolves.
 *  The list invalidation stays with the policy — it is not hand-wired here. */
/** `onDeleted` runs the moment the server has agreed, before the cache's
 *  invalidation is awaited: a surface that leaves the deleted chat must not
 *  wait on refetches of that chat's own reads, and must not leave at all when
 *  the server refuses. */
export function useDeleteChat(onDeleted?: (chatId: string) => void) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (chatId: string) => {
      const deleted = await deleteChat(chatId);
      onDeleted?.(chatId);
      for (const queryKey of [
        // The row, and by prefix its messages and its attachments.
        keys.chats.one(chatId),
        keys.chatWorkspace.one(chatId),
        chatKeys.turns(chatId),
        activityKeys.decisions(chatId),
        activityKeys.safety(chatId),
        activityKeys.cost(chatId),
        activityKeys.ledger(chatId),
      ]) {
        qc.removeQueries({ queryKey });
      }
      return deleted;
    },
    meta: { invalidates: [keys.chats.all, keys.workspaces.all] },
  });
}

// --- attachments -------------------------------------------------------------
//
// A chat attachment is a Files NODE linked to the chat, never a copy of the
// bytes: the transcript names the node and every read of it is authorized
// again, per request, against the caller. That is why the list below is a
// query rather than a field on the chat row — two readers of the same chat see
// different attachments, and the one the server omits is the one this reader
// may not read.
//
// The shapes come from the generated document, so the wire and the reader can
// never drift apart: the list answers an ENVELOPE (`{items: [...]}`), which is
// why the read below unwraps it instead of handing the body through.

/** What a reader can do with a linked node right now.
 *
 *  `unavailable` is the server saying "this chat holds a reference you may not
 *  read": the row still arrives — a member already knows the reference exists —
 *  but it carries none of the node's facts. */
export type ChatAttachmentState = "available" | "unavailable";

/** One linked node, as the caller can read it. */
export type ChatAttachmentRead = components["schemas"]["ChatAttachmentRead"];
/** The list answer: the server wraps the rows in an envelope. */
type ChatAttachmentList = components["schemas"]["ChatAttachmentList"];
type ChatAttachmentCreate = components["schemas"]["ChatAttachmentCreate"];

interface LinkAttachmentVars {
  chatId: string;
  nodeId: string;
}

/** One request, its refusal raised. Split from {@link attachmentsRequest} because
 *  the unlink answers `204` with no body at all, and a `.json()` on that is a
 *  parse error dressed up as a failed unlink. */
async function attachmentsSend(path: string, init: RequestInit = {}): Promise<Response> {
  const response = await apiFetch(new URL(path, apiBaseUrl).toString(), {
    credentials: "include",
    ...init,
  });
  if (!response.ok) throw await failedResponse(response);
  return response;
}

async function attachmentsRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  return (await (await attachmentsSend(path, init)).json()) as T;
}

function attachmentsPath(chatId: string): string {
  return `/api/v1/chats/${encodeURIComponent(chatId)}/attachments`;
}

function attachmentPath(chatId: string, nodeId: string): string {
  return `${attachmentsPath(chatId)}/${encodeURIComponent(nodeId)}`;
}

/** The nodes linked to this chat that the caller can read. Unreadable ones are
 *  omitted by the server, so an empty answer is a real answer. */
export function useChatAttachments(chatId: string | undefined) {
  return useQuery<ChatAttachmentRead[]>({
    queryKey: keys.chats.attachments(chatId),
    queryFn: async () =>
      (await attachmentsRequest<ChatAttachmentList>(attachmentsPath(chatId as string))).items,
    enabled: Boolean(chatId),
  });
}

/** Link one already-uploaded node to the chat. The server decides — the chat's
 *  SEND and a Files READ on the node — so this is never a claim, it is a
 *  request. Narrowed to the chat's own keys: linking a file has nothing to say
 *  about the rail, the machine, or anything else the portal is holding. */
export function useLinkAttachment() {
  return useMutation<ChatAttachmentRead, ApiError, LinkAttachmentVars>({
    meta: { invalidates: [keys.chats.all] },
    mutationFn: (vars) =>
      attachmentsRequest<ChatAttachmentRead>(attachmentsPath(vars.chatId), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ nodeId: vars.nodeId } satisfies ChatAttachmentCreate),
      }),
  });
}

/** Unlink one node from the chat. The server answers `204` and nothing else, so
 *  the caller learns only that the reference is gone — which is the whole fact.
 *
 *  A refusal rejects rather than resolving, so the surface that asked can keep
 *  the thing on screen and say why instead of dropping a chip over an unlink
 *  that never happened. */
export function useUnlinkAttachment() {
  return useMutation<void, ApiError, LinkAttachmentVars>({
    meta: { invalidates: [keys.chats.all] },
    mutationFn: async (vars) => {
      await attachmentsSend(attachmentPath(vars.chatId, vars.nodeId), { method: "DELETE" });
    },
  });
}

// --- the workspace beside a chat ---------------------------------------------
//
// Which tabs this reader has open beside this chat, and which one was in front.
// Per reader AND per chat: two people in one chat have different tabs open and
// the server answers each of them their own, so the entry sits under its own
// slot rather than riding the chat row.
//
// The document is declared free-form on the wire, so the shape a caller can
// rely on is spelled here: `tabs` and `active_tab_id` are what this build
// reads, and every other field a newer writer left in it rides back out unread
// rather than being dropped.

/** One stored tab. Ids only — a tab names a node, never a URL or bytes.
 *
 *  A field a newer build wrote into a tab is not spelled here and is not read,
 *  but it is carried: the document is round-tripped as it arrived, so an older
 *  client never strips what a newer one is relying on. */
export interface WorkspaceTabWire {
  id: string;
  kind: string;
  node_id?: string | null;
  name: string;
  path?: string | null;
  params?: Record<string, unknown>;
  /** The editor group the tab is in. Absent on a tab a build without groups
   *  opened; such a tab is read into the group the reader was working in. */
  group?: string;
  /** The way the tab shows its file (`edit`, `preview`, …). Absent: the file's
   *  default. */
  view?: string;
  /** A preview tab, replaced by the next file opened the same way. */
  transient?: boolean;
}

/** One node of the stored arrangement of editor groups: a group, or a split of
 *  its children along one axis with a share of the space each. */
export type WorkspaceLayoutNodeWire =
  | { g: string }
  | { split: "row" | "column"; children: WorkspaceLayoutNodeWire[]; sizes: number[] };

/** How the editor groups are arranged. Versioned on its own: a reader that does
 *  not know `v` reads the groups the tabs name into a row instead. */
export interface WorkspaceLayoutWire {
  v: number;
  root: WorkspaceLayoutNodeWire;
  /** The group the reader was working in. */
  active_group?: string;
  /** The tab in front of each group, by group id. */
  active?: Record<string, string>;
}

/** The stored workspace document. `active_tab_id` is the tab in front of the
 *  group the reader was working in, which is all a build without groups reads. */
export interface ChatWorkspaceDoc {
  tabs: WorkspaceTabWire[];
  active_tab_id?: string | null;
  layout?: WorkspaceLayoutWire;
}

export interface ChatWorkspaceRead {
  state: ChatWorkspaceDoc;
  updated_at: string | null;
}

export interface SaveWorkspaceVars {
  chatId: string;
  state: ChatWorkspaceDoc;
  /** Let the write outlive the page. Used on the way out — a tab being hidden,
   *  a chat being left — where a normal request would simply be cancelled. */
  keepalive?: boolean;
}

function workspacePath(chatId: string): string {
  return `/api/v1/chats/${encodeURIComponent(chatId)}/workspace`;
}

async function workspaceRequest(path: string, init: RequestInit = {}): Promise<ChatWorkspaceRead> {
  const response = await apiFetch(new URL(path, apiBaseUrl).toString(), {
    credentials: "include",
    ...init,
  });
  if (!response.ok) throw await failedResponse(response);
  return (await response.json()) as ChatWorkspaceRead;
}

/** Replace this reader's workspace for this chat.
 *
 *  A plain function rather than only a hook because it is called from outside
 *  React: the workspace store debounces its own writes and flushes the last one
 *  as the page goes away, and neither of those can be done from a hook. */
export async function putChatWorkspace(vars: SaveWorkspaceVars): Promise<ChatWorkspaceRead> {
  return workspaceRequest(workspacePath(vars.chatId), {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ state: vars.state }),
    keepalive: vars.keepalive ?? false,
  });
}

/** This reader's tabs beside this chat. A chat nobody has laid out yet answers
 *  the empty document, so there is no "never saved" state to carry. */
export function useWorkspaceState(chatId: string | undefined) {
  return useQuery<ChatWorkspaceRead>({
    queryKey: keys.chatWorkspace.one(chatId),
    enabled: Boolean(chatId),
    queryFn: () => workspaceRequest(workspacePath(chatId as string)),
    // The layout is per-reader server state with no event of its own, so the
    // only other writer is this same person in another tab or on another
    // device. Coming back to a tab is exactly when they may have moved it
    // somewhere else, and is the one moment worth spending a read on — the rest
    // of the time this is last-writer-wins, deliberately.
    refetchOnWindowFocus: true,
  });
}

/** Save the layout.
 *
 *  It invalidates nothing, deliberately. Opening a tab says nothing about the
 *  chat, the rail, the machine or the drive, and this mutation runs at the rate
 *  a person clicks — under the blanket policy every one of those clicks would
 *  refetch the whole page. The one entry whose new value IS known is seeded
 *  from the server's own answer instead, so the next reader of the layout gets
 *  it without a round trip. */
export function useSaveWorkspace() {
  const queryClient = useQueryClient();
  return useMutation<ChatWorkspaceRead, ApiError, SaveWorkspaceVars>({
    meta: { invalidates: "none" },
    mutationFn: putChatWorkspace,
    onSuccess: (data, vars) => {
      queryClient.setQueryData(keys.chatWorkspace.one(vars.chatId), data);
    },
  });
}

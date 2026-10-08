// The cloud chat REST calls, in one place.
//
// These go out through `apiFetch` on the same origin with the session cookie,
// the way every other portal request authenticates (no token is ever placed in
// a URL), with the org assertion and the session renewal the typed client has.
// They are held apart from `CloudDataSource` so a test can drive either half on
// its own: the source against a stubbed transport, these against a stubbed
// `fetch`.

import type { components } from "@alkera/sdk";

import { apiBaseUrl, apiFetch, failedResponse } from "../client";

// The shapes these calls return, exactly as the server spells them: generated
// from `openapi.json` into `@alkera/sdk`, so a route that changes shape fails a
// build here rather than a reader in a tab. Named once, at the boundary that
// receives them; every consumer imports from here.
type Schemas = components["schemas"];
export type ChatSessionRead = Schemas["ChatSessionRead"];
export type ChatWakeRead = Schemas["ChatWakeRead"];
export type ChatReadStateRead = Schemas["ChatReadStateRead"];
export type ChatSessionList = Schemas["ChatSessionList"];
export type ChatMessageRead = Schemas["ChatMessageRead"];
export type ChatMessageList = Schemas["ChatMessageList"];
export type ChatMessageCreate = Schemas["ChatMessageCreate"];
export type ChatInterruptAnswer = Schemas["ChatInterruptAnswer"];
export type ChatPermissionModeUpdate = Schemas["ChatPermissionModeUpdate"];
export type ChatModelRead = Schemas["ChatModelRead"];
export type ChatModelList = Schemas["ChatModelList"];
export type ChatModelOptionsRead = Schemas["ChatModelOptions"];
export type ChatDefaultsRead = Schemas["ChatDefaultsRead"];
export type ChatPermissionMode = ChatSessionRead["permission_mode"];
export type ChatPromoteRequest = Schemas["ChatPromoteRequest"];
export type PromoteColumn = Schemas["PromoteColumn"];
export type WorkspaceObjectRead = Schemas["WorkspaceObjectRead"];
export type WorkspaceObjectList = Schemas["WorkspaceObjectList"];
export type WorkspaceObjectCreate = Schemas["WorkspaceObjectCreate"];
export type WorkspaceObjectUpdate = Schemas["WorkspaceObjectUpdate"];
export type WorkspaceObjectType = WorkspaceObjectRead["type"];
export type ObjectRowsPage = Schemas["ObjectRowsPage"];
/** A create naming the saved-query kind, which the objects route retired: what
 *  it mints narrowed to a result, and a create still naming a query is answered
 *  410. Spelled here rather than cast at the callsite, so the one surface still
 *  offering it — the chat's "Save as query" — says what it is asking for, and
 *  this name goes when that surface does. */
export type RetiredQueryCreate = Omit<WorkspaceObjectCreate, "type"> & { type: "query" };
/** The org's workspace machine as `GET /api/v1/machines/current` reports it. */
export type MachineStateRead = Schemas["MachineStateRead"];
/** How a chat's machine is doing, as the chat row or the live machine reports
 *  it — `none` when it has none. The live machine alone says `restarting`. */
export type ChatMachineStatus = ChatSessionRead["machine_status"] | MachineStateRead["status"];

const JSON_HEADERS = { "content-type": "application/json" } as const;

/** The absolute URL of an API path, with `query` appended when it has entries. */
export function apiUrl(
  path: string,
  query: Record<string, string | number | undefined> = {},
): string {
  const url = new URL(path, apiBaseUrl);
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== "") url.searchParams.set(key, String(value));
  }
  return url.toString();
}

async function send<T>(
  path: string,
  init: RequestInit & { query?: Record<string, string | number | undefined> } = {},
): Promise<T> {
  const { query, ...rest } = init;
  // Through the read gate, like every typed read: these calls are the chat
  // page's busiest polls, and a page whose SDK reads honour a refusal while its
  // chat reads ignore it is a page that keeps the limiter refusing. The gate
  // holds reads only — the method here decides, and a write goes straight out.
  const response = await apiFetch(apiUrl(path, query), { credentials: "include", ...rest });
  if (!response.ok) throw await failedResponse(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const postJson = <T>(path: string, payload: unknown): Promise<T> =>
  send<T>(path, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(payload) });

const putJson = <T>(path: string, payload: unknown): Promise<T> =>
  send<T>(path, { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify(payload) });

/** A POST whose success carries no body to read. Kept apart from `postJson`
 *  rather than sniffed for, because parsing "nothing" as JSON throws and the
 *  throw would read to the caller as a failed request. */
async function postAccepted(path: string, payload: unknown): Promise<void> {
  const response = await apiFetch(apiUrl(path), {
    credentials: "include",
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });
  if (response.ok) return;
  throw await failedResponse(response);
}

// --- chats -------------------------------------------------------------------

export const listChats = (
  opts: { limit?: number; cursor?: string } = {},
): Promise<ChatSessionList> =>
  send<ChatSessionList>("/api/v1/chats", {
    // No limit of our own: the server's default page is sized for the whole of
    // an ordinary org, and a number spelled here would be a second ceiling to
    // keep in step with it.
    query: { limit: opts.limit, cursor: opts.cursor },
  });

/** A page is more than every reader has to know about.
 *
 *  The rail, the chat home, the dashboard and the breadcrumb lookups all want
 *  "the chats", so this follows the cursor the API returns until it is spent;
 *  stopping at the first page would make an org's fifty-first chat read like
 *  one somebody had deleted.
 *
 *  A short page is not the end: the server cuts each page to what the caller
 *  may read AFTER taking it, so a page can come back with fewer rows than it
 *  asked for and still have more behind it. Only a missing `next_cursor` ends
 *  the walk. `PAGE_CEILING` is the guard against a server that keeps handing
 *  one back — a bounded read beats a tab that spins forever. */
const PAGE_CEILING = 100;

/** The walk in flight, if one is. Two readers of the same list — the portal's
 *  own `useChats` and the chat host behind the rail, which hold separate cache
 *  entries because they answer different shapes — ask for it in the same tick
 *  whenever the event stream stales them together, and the second ask can only
 *  ever get what the first is already fetching. Joining it halves the page's
 *  busiest request. A walk that fails is not remembered: the next caller starts
 *  a fresh one. */
let listWalk: { reader: ChatPageReader; since: number; walk: Promise<ChatSessionList> } | null =
  null;

/** Bumped by anything that makes an in-flight answer the WRONG answer: a write
 *  this client made, and a change of who is signed in. A walk that started
 *  before the bump is not joined, because it was started before the thing the
 *  joiner is asking about. Without it a refetch triggered BY a delete joins the
 *  walk that began before it and shows the chat back in the rail for a round
 *  trip, and a sign-in inside the window is handed the previous account's list. */
let listGeneration = 0;

export function chatListChanged(): void {
  listGeneration += 1;
  listWalk = null;
}

/** `fetchPage` is the one page read, injectable so the coalescing above can be
 *  driven without a network. */
export type ChatPageReader = (params: { cursor?: string }) => Promise<ChatSessionList>;

export function listAllChats(fetchPage: ChatPageReader = listChats): Promise<ChatSessionList> {
  // Only a caller reading the list the same way, through a walk that began
  // after the last thing that changed it, may join.
  if (listWalk !== null && listWalk.reader === fetchPage && listWalk.since === listGeneration) {
    return listWalk.walk;
  }
  const since = listGeneration;
  const walk = walkAllChats(fetchPage).finally(() => {
    if (listWalk?.walk === walk) listWalk = null;
  });
  listWalk = { reader: fetchPage, since, walk };
  return walk;
}

async function walkAllChats(fetchPage: ChatPageReader): Promise<ChatSessionList> {
  const items: ChatSessionRead[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < PAGE_CEILING; page += 1) {
    const next = await fetchPage({ cursor });
    items.push(...next.items);
    if (!next.next_cursor) return { items, next_cursor: null };
    cursor = next.next_cursor;
  }
  return { items, next_cursor: cursor ?? null };
}

/** What a create may name besides its title. Every field here is a wire field
 *  of `POST /api/v1/chats` — a caller that spells a key this type does not have
 *  is a type error, never a silently dropped one. */
export interface CreateChatOptions {
  model?: string;
  effort?: string;
  /** The stance the chat opens in. Omitted, the server resolves the reader's
   *  saved default — so this is named only when the composer showed one. */
  permissionMode?: ChatPermissionMode;
  sourceNodeId?: string;
  /** Take the chat warmed ahead for this reader instead of creating one. The
   *  empty composer's first send says so; with no spare standing the server
   *  creates as usual, so the answer is the same shape either way. */
  claimSpare?: boolean;
  /** The workspace to start the chat in. Omitted, the server decides: a
   *  workspace of its own, or the caller's main workspace once a workspace may
   *  hold several chats. Never sent with `claimSpare`: a spare is warmed in no
   *  workspace in particular. */
  workspaceId?: string;
}

/** Open a chat. `model`/`effort` are the reader's pick from `chatModels()`; the
 *  server resolves them against the same catalog and pins what it resolved, so
 *  a stale id is refused here rather than failing on the chat's first turn.
 *  `permissionMode` rides the same request: the stance is decided when the row
 *  is written and the box reads it off that row when it opens the session, so a
 *  stance that does not reach this body is not the stance the first turn runs
 *  in. */
export const createChat = (
  title: string | null,
  opts: CreateChatOptions = {},
): Promise<ChatSessionRead> => {
  chatListChanged();
  return postJson<ChatSessionRead>("/api/v1/chats", {
    title,
    ...(opts.model ? { model: opts.model } : {}),
    ...(opts.effort ? { effort: opts.effort } : {}),
    ...(opts.permissionMode ? { permission_mode: opts.permissionMode } : {}),
    ...(opts.sourceNodeId ? { source_node_id: opts.sourceNodeId } : {}),
    ...(opts.claimSpare && !opts.workspaceId ? { claim_spare: true } : {}),
    ...(opts.workspaceId ? { workspace_id: opts.workspaceId } : {}),
  });
};

/** What the warm call left standing for this reader. */
export interface ChatSpareState {
  state: "warm" | "none";
}

/** The chat page's heartbeat: keep one chat warmed ahead for this reader, its
 *  session already open on the box, so the first message of a new chat does
 *  not wait for a spawn. Idempotent; `none` is a state, never an error the
 *  page shows. */
export const warmSpare = (): Promise<ChatSpareState> =>
  postJson<ChatSpareState>("/api/v1/chats/spare", {});

/** Put the chat's session into a permission mode. Returns the chat as stored,
 *  so the caller's pill follows what the server accepted and never its own
 *  optimistic guess. */
export const setPermissionMode = (
  chatId: string,
  mode: ChatPermissionMode,
): Promise<ChatSessionRead> =>
  putJson<ChatSessionRead>(`/api/v1/chats/${encodeURIComponent(chatId)}/permission-mode`, { mode });

/** Move an open chat onto a model. Resolved against the same catalog a create
 *  is, so a model the workspace cannot run is a 422 here rather than a pin the
 *  box discovers it cannot honour on the next turn. Returns the chat as stored,
 *  so the chip follows what the server accepted. */
export const setChatModel = (
  chatId: string,
  model: string,
  effort?: string | null,
  expectedModelId?: string | null,
): Promise<ChatSessionRead> =>
  putJson<ChatSessionRead>(`/api/v1/chats/${encodeURIComponent(chatId)}/model`, {
    model,
    ...(effort ? { effort } : {}),
    ...(expectedModelId ? { expected_model_id: expectedModelId } : {}),
  });

/** Every model this reader may pick for an open chat, each with whether the
 *  chat may move to it (a chat whose reasoning another model cannot read stays
 *  where it is) and the reason when not. Never fails on a catalog outage: the
 *  list is empty instead. */
export const chatModelOptions = (chatId: string): Promise<ChatModelOptionsRead> =>
  send<ChatModelOptionsRead>(`/api/v1/chats/${encodeURIComponent(chatId)}/model-options`);

export const getChat = (chatId: string): Promise<ChatSessionRead> =>
  send<ChatSessionRead>(`/api/v1/chats/${encodeURIComponent(chatId)}`);

/** Asks for the wake opening a chat stands for. The server decides who may
 *  ask and answers at most one wake per chat per interval. */
export const wakeChat = (chatId: string): Promise<ChatWakeRead> =>
  send<ChatWakeRead>(`/api/v1/chats/${encodeURIComponent(chatId)}/wake`, { method: "POST" });

/** The same wake for a workspace opened with no chat: the server picks its
 *  most recently active chat this reader may send in. Nothing comes back when
 *  the workspace holds none. */
export const wakeWorkspace = (workspaceId: string): Promise<ChatWakeRead | undefined> =>
  send<ChatWakeRead | undefined>(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}/wake`, { method: "POST" });

/** Moves the caller's own read mark forward to `seq`, the highest transcript
 *  sequence the page has shown. The server never moves it backward. */
export const markChatRead = (chatId: string, seq: number): Promise<ChatReadStateRead> =>
  send<ChatReadStateRead>(`/api/v1/chats/${encodeURIComponent(chatId)}/read`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ seq }),
  });

/** Marks the chat unread for the caller until they next read it. */
export const markChatUnread = (chatId: string): Promise<ChatReadStateRead> =>
  send<ChatReadStateRead>(`/api/v1/chats/${encodeURIComponent(chatId)}/unread`, { method: "POST" });

/** Marks every chat in the workspace read, for the caller alone. */
export const markWorkspaceRead = (workspaceId: string): Promise<void> =>
  send<void>(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}/read`, { method: "POST" });

/** Takes the chat out of the workspace; a 204 with nothing to read. */
export const deleteChat = (chatId: string): Promise<void> => {
  chatListChanged();
  return send<void>(`/api/v1/chats/${encodeURIComponent(chatId)}`, { method: "DELETE" });
};

/** A page of transcript. Forward from `afterSeq` (the live tail's read), or —
 *  one of `tail` / `before` — the newest page and the pages below it, which is
 *  how a long chat opens on its last turn and scrolls up. The server refuses a
 *  page asked for in two directions at once. */
export const listMessages = (
  chatId: string,
  opts: { afterSeq?: number; limit?: number; before?: number; tail?: boolean } = {},
): Promise<ChatMessageList> =>
  send<ChatMessageList>(`/api/v1/chats/${encodeURIComponent(chatId)}/messages`, {
    query: {
      limit: opts.limit ?? 200,
      ...(opts.tail ? { tail: "true" } : {}),
      ...(opts.before !== undefined ? { before: opts.before } : {}),
      ...(opts.tail || opts.before !== undefined ? {} : { after_seq: opts.afterSeq ?? 0 }),
    },
  });

export const postMessage = (chatId: string, body: ChatMessageCreate): Promise<ChatMessageRead> =>
  postJson<ChatMessageRead>(`/api/v1/chats/${encodeURIComponent(chatId)}/messages`, body);

/** Answer an ask the agent is blocked on. Relayed to the machine, never
 *  recorded here: the resolution enters the transcript when the harness settles
 *  it, so this returns nothing and the reader sees the answer land as an event.
 *  202 means the relay was put on the chat's channel — whether it settles the
 *  ask is the machine's decision (an ask that has already gone is dropped). */
export const answerInterrupt = (chatId: string, body: ChatInterruptAnswer): Promise<void> =>
  postAccepted(`/api/v1/chats/${encodeURIComponent(chatId)}/answer`, body);

/** End the turn this chat's session is running. Relayed to the machine, so a
 *  202 means the stop was put on the chat's channel: a box running the chat
 *  cancels its turn, and the transcript keeps the line naming who stopped it. */
export const stopTurn = (chatId: string): Promise<void> =>
  postAccepted(`/api/v1/chats/${encodeURIComponent(chatId)}/stop`, {});

export const promoteResult = (
  chatId: string,
  body: ChatPromoteRequest,
): Promise<WorkspaceObjectRead> =>
  postJson<WorkspaceObjectRead>(`/api/v1/chats/${encodeURIComponent(chatId)}/promote`, body);

// --- the reader's own chat settings ------------------------------------------

/** The models this reader may start a chat on, as the gateway serves them. An
 *  EMPTY list means the catalog could not be read, not that there are none —
 *  the composer polls while it is empty, exactly as the editor's picker does. */
export const chatModels = (): Promise<ChatModelList> =>
  send<ChatModelList>("/api/v1/me/chat-models");

/** The saved Default Chat Model + Effort, resolved server-side against the live
 *  catalog (a gateway outage returns the saved values untouched). */
export const chatDefaults = (): Promise<ChatDefaultsRead> =>
  send<ChatDefaultsRead>("/api/v1/me/chat-defaults");

// --- machines ----------------------------------------------------------------

/** The machine the caller's next chat would run on; with `workspaceId`, a chat
 *  of that workspace (a workspace pinned to an org machine runs there). */
export const currentMachine = (workspaceId?: string): Promise<MachineStateRead> =>
  send<MachineStateRead>("/api/v1/machines/current", workspaceId ? { query: { workspace_id: workspaceId } } : {});

// --- objects -----------------------------------------------------------------

export const listObjects = (
  opts: { type?: WorkspaceObjectType; limit?: number; cursor?: string } = {},
): Promise<WorkspaceObjectList> =>
  send<WorkspaceObjectList>("/api/v1/objects", {
    query: { type: opts.type, limit: opts.limit, cursor: opts.cursor },
  });

export const createObject = (
  body: WorkspaceObjectCreate | RetiredQueryCreate,
): Promise<WorkspaceObjectRead> => postJson<WorkspaceObjectRead>("/api/v1/objects", body);

export const getObject = (objectId: string): Promise<WorkspaceObjectRead> =>
  send<WorkspaceObjectRead>(`/api/v1/objects/${encodeURIComponent(objectId)}`);

/** Edit an object, naming the version that was read.
 *
 *  `expected_version` is compared under the row's lock server-side, so a write
 *  that names a version the row has moved past is answered 409 rather than
 *  quietly overwriting whatever landed in between. */
export const updateObject = (
  objectId: string,
  body: WorkspaceObjectUpdate,
): Promise<WorkspaceObjectRead> =>
  putJson<WorkspaceObjectRead>(`/api/v1/objects/${encodeURIComponent(objectId)}`, body);

export const objectRows = (
  objectId: string,
  opts: { offset?: number; limit?: number } = {},
): Promise<ObjectRowsPage> =>
  send<ObjectRowsPage>(`/api/v1/objects/${encodeURIComponent(objectId)}/rows`, {
    query: { offset: opts.offset ?? 0, limit: opts.limit ?? 50 },
  });

/** Where the browser downloads a result's CSV from. The cookie rides the
 *  navigation, so nothing authenticates in the URL. */
export const objectCsvUrl = (objectId: string): string =>
  apiUrl(`/api/v1/objects/${encodeURIComponent(objectId)}/export.csv`);

/** Re-run a saved query. The route was retired with the kind, so the server
 *  declares neither it nor the shape it answered — both are spelled here, and
 *  the whole call goes with the saved-query page that is its only caller. Until
 *  then nothing here may claim to address a declared route. */
type RetiredRerunAccepted = { run_id: string; chat_id: string };

export const rerunObject = (
  objectId: string,
  params: Record<string, unknown>,
): Promise<RetiredRerunAccepted> =>
  postJson<RetiredRerunAccepted>(`/api/v1/objects/${encodeURIComponent(objectId)}/rerun`, {
    params,
  });

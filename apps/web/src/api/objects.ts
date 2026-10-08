// Workspace objects: the saved queries and promoted results a chat leaves
// behind, and the rows a result holds.
//
// A promoted result's payload is uploaded by the machine that produced it, so a
// freshly promoted object is `pending_upload` for a moment and becomes `ready`
// when a `workspace_object.changed` frame lands — which is why the object's own
// key and the list are both named in `EVENT_KEYS`, and why nothing here polls.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { chatKeys } from "@/pages/workspace/chat/chatKeys";

import { ApiError, refusalCopy } from "./errors";
import { keys } from "./keys";
import {
  getObject,
  listObjects,
  objectRows,
  rerunObject,
  updateObject,
  type ObjectRowsPage,
  type WorkspaceObjectList,
  type WorkspaceObjectRead,
  type WorkspaceObjectType,
} from "./cloudChat/transport";

export type {
  ObjectRowsPage,
  WorkspaceObjectRead,
  WorkspaceObjectType,
} from "./cloudChat/transport";
export { objectCsvUrl } from "./cloudChat/transport";

export const OBJECT_ROWS_PAGE = 50;

/** The biggest page the rows route serves (`MAX_ROW_LIMIT` in
 *  `alkera_core/schemas/objects/api.py`) — what a chart reads a page at a
 *  time, so a 90-point series is one request rather than two. */
export const OBJECT_ROWS_MAX_PAGE = 1000;

/** The most points a chart will draw. A time series longer than this is not a
 *  chart any more, and reading it would be an unbounded number of requests
 *  against a result the reader is only glancing at. The table stays paged and
 *  the CSV stays complete, so nothing here is the only way to the data. */
export const CHART_MAX_POINTS = 5000;

/** The workspace's objects of one kind, newest first.
 *
 *  The kind rides the key: results and queries are two lists on screen at once,
 *  and one key for both would have the second read overwrite the first in the
 *  cache and show a page of the wrong kind.
 *
 *  No page calls this today — the Files tree lists the saved queries and reports
 *  now, through the drive's own listing. Kept because the endpoint it reads is
 *  still the API's, and a picker that needs "every saved query" wants this read
 *  rather than a second spelling of the same key. */
export function useObjects(type?: WorkspaceObjectType) {
  return useQuery<WorkspaceObjectList>({
    queryKey: type ? keys.objects.ofType(type) : keys.objects.all,
    queryFn: () => listObjects({ type }),
  });
}

export function useObject(objectId: string | undefined) {
  return useQuery<WorkspaceObjectRead>({
    queryKey: keys.objects.one(objectId),
    queryFn: () => getObject(objectId as string),
    enabled: Boolean(objectId),
  });
}

/** One page of a result's rows. The page rides the key, so paging is a cache
 *  entry per page rather than a refetch that discards the one on screen. */
/** A result's rows.
 *
 *  `ready` gates the read: a promoted result exists before its payload does, and
 *  the rows route answers 409 `payload_pending` until the machine delivers —
 *  four console errors per visit for a result that was never coming. `ready`
 *  defaults to true so a caller that knows the object is ready (or does not
 *  track its status) reads as before. */
export function useObjectRows(
  objectId: string | undefined,
  offset = 0,
  limit = OBJECT_ROWS_PAGE,
  ready = true,
) {
  return useQuery<ObjectRowsPage>({
    queryKey: keys.objects.rows(objectId, offset, limit),
    queryFn: () => objectRows(objectId as string, { offset, limit }),
    enabled: Boolean(objectId) && ready,
  });
}

/** Every row a chart needs, paged through until the result is exhausted.
 *
 *  The rows route pages (50 by default, 1000 at most) and the table on the
 *  object page draws one page at a time, which is right for a table and wrong
 *  for a chart: a 90-day series drawn from the first page is a 50-day series
 *  with nothing on screen to say the rest is missing. So the chart reads the
 *  whole result — bounded by `CHART_MAX_POINTS`, past which a line is not
 *  telling anyone anything anyway.
 *
 *  One cache entry, not one per page: the chart wants the series, and a
 *  half-read series is not a smaller chart, it is a wrong one. */
export function useObjectChartRows(objectId: string | undefined, enabled = true) {
  return useQuery<ObjectRowsPage>({
    queryKey: keys.objects.chartRows(objectId),
    queryFn: async () => {
      const id = objectId as string;
      const first = await objectRows(id, { offset: 0, limit: OBJECT_ROWS_MAX_PAGE });
      const rows = [...first.rows];
      const wanted = Math.min(first.total, CHART_MAX_POINTS);
      while (rows.length < wanted) {
        const next = await objectRows(id, {
          offset: rows.length,
          limit: Math.min(OBJECT_ROWS_MAX_PAGE, wanted - rows.length),
        });
        // A page that answers nothing ends the read: a total that disagrees
        // with what the store holds must not spin.
        if (next.rows.length === 0) break;
        rows.push(...next.rows);
      }
      return { ...first, rows: rows.slice(0, CHART_MAX_POINTS) };
    },
    enabled: Boolean(objectId) && enabled,
  });
}

/** Re-run a saved query with different parameters. The cloud never executes
 *  SQL: this relays the run to the chat the object is bound to, and the answer
 *  arrives in that chat's transcript. */
export function useRerunObject(objectId: string | undefined) {
  return useMutation({
    mutationFn: (params: Record<string, unknown>) => rerunObject(objectId as string, params),
    meta: { invalidates: [keys.objects.one(objectId)] },
  });
}

/** What the server says about a title with nothing in it — pydantic's own
 *  wording for the `min_length=1` an object title carries. Spelled here so a
 *  rename refused before it leaves the browser reads exactly as a rename
 *  refused by the API, rather than inventing a second sentence for one rule. */
export const EMPTY_TITLE_REFUSAL = "String should have at least 1 character";

/** What a refused rename says, in the reader's own words.
 *
 *  A conflict gets this module's sentence rather than the route's ("This object
 *  moved on; re-read it and try again"), because the re-read the route asks for
 *  has already happened by the time anyone reads this — the hook fires it. Every
 *  other refusal keeps the server's own sentence, which `ApiError` has already
 *  pulled out of the envelope, so a policy that names a reason says it here. */
export function renameRefusal(error: unknown): string {
  return refusalCopy(error, {
    conflict: "This chat changed underneath you. It has been re-read; try the rename again.",
    forbidden: "You cannot rename this chat.",
    fallback: "This chat could not be renamed.",
  });
}

export interface RenameChatInput {
  readonly chatId: string;
  readonly title: string;
}

interface RenameSnapshot {
  readonly chat: unknown;
  readonly object: unknown;
  readonly list: unknown;
  /** The chat surface's own copy of the list. It is a second cache entry of the
   *  same rows, read by a composition that has to work in the editor too, so a
   *  writer here has to name it as well. */
  readonly surface: unknown;
}

/** Rename a chat.
 *
 *  A chat IS a workspace object — the Files facet's `object.id` is the chat id —
 *  so the title is written through the object route's WRITE gate, its version
 *  check and its decision row. There is no second rename endpoint to keep in
 *  step, and the `.alkerachat` folder name the node was born with is not
 *  touched: the row renders the object's title (`displayNameOf`).
 *
 *  The version is read here rather than carried in by each caller: the rail, the
 *  chat header and a Files row all know the title on screen and none of them
 *  holds the object's version, and a version guessed from a stale list is a 409
 *  the reader did nothing to deserve.
 *
 *  The new title is on screen before the request leaves: `onMutate` patches
 *  EVERY entry the title is read from — the chat read, the portal's chat list,
 *  the object read, and the chat surface's own list — and `onError` restores
 *  exactly the snapshot it took. Refreshing the Files listings is left to the
 *  mutation policy in `createQueryClient` via `meta.invalidates` — never a
 *  hand-wired `invalidateQueries` here.
 *
 *  That last entry is the one this hook kept forgetting. The chat composition
 *  is shared with the editor's webview, so it cannot read the portal's cache: it
 *  lists chats under `chatKeys.chats()`, and the open chat's header, its crumb
 *  trail and the rail inside it all take the title from there. Patching only the
 *  portal's entries renamed the browser tab and the rail while the chat's own
 *  header kept the old name until the page was reloaded.
 */
export function useRenameChat() {
  const qc = useQueryClient();
  return useMutation<WorkspaceObjectRead, unknown, RenameChatInput, RenameSnapshot>({
    mutationFn: async ({ chatId, title }: RenameChatInput) => {
      const current = await getObject(chatId);
      return updateObject(chatId, { title, expected_version: current.version });
    },
    onMutate: async ({ chatId, title }) => {
      // Both lists too: a read already on the wire answers with the old title,
      // and landing after the patch it would put the old name back on screen.
      await Promise.all([
        qc.cancelQueries({ queryKey: keys.chats.one(chatId) }),
        qc.cancelQueries({ queryKey: chatKeys.chats() }),
      ]);
      const snapshot: RenameSnapshot = {
        chat: qc.getQueryData(keys.chats.one(chatId)),
        object: qc.getQueryData(keys.objects.one(chatId)),
        list: qc.getQueryData(keys.chats.all),
        surface: qc.getQueryData(chatKeys.chats()),
      };
      const retitle = (old: unknown): unknown =>
        old && typeof old === "object" ? { ...(old as object), title } : old;
      qc.setQueryData(keys.chats.one(chatId), retitle);
      qc.setQueryData(keys.objects.one(chatId), retitle);
      qc.setQueryData(keys.chats.all, (old: unknown) => {
        const list = old as { items?: readonly { id: string }[] } | undefined;
        if (!list?.items) return old;
        return {
          ...list,
          items: list.items.map((item) => (item.id === chatId ? { ...item, title } : item)),
        };
      });
      // The surface's list is a bare array of its own row shape, so it is
      // patched by id rather than through `retitle`.
      qc.setQueryData(chatKeys.chats(), (old: unknown) => {
        const rows = old as readonly { id: string }[] | undefined;
        if (!Array.isArray(rows)) return old;
        return rows.map((row) => (row.id === chatId ? { ...row, title } : row));
      });
      return snapshot;
    },
    onError: (error, { chatId }, snapshot) => {
      if (snapshot) {
        qc.setQueryData(keys.chats.one(chatId), snapshot.chat);
        qc.setQueryData(keys.objects.one(chatId), snapshot.object);
        qc.setQueryData(keys.chats.all, snapshot.list);
        qc.setQueryData(chatKeys.chats(), snapshot.surface);
      }
      // A conflict means somebody else's title is the real one: re-read it so
      // the next attempt starts from what the row actually holds, and so the
      // reader is looking at the live title while they decide.
      if (error instanceof ApiError && error.status === 409) {
        void qc.refetchQueries({ queryKey: keys.objects.one(chatId) });
        void qc.refetchQueries({ queryKey: keys.chats.one(chatId) });
      }
    },
    // The surface's list rides here too: the optimistic patch puts the new name
    // on screen, this is what makes the server's word land on the entry the
    // header reads rather than only on the portal's.
    meta: {
      invalidates: [keys.chats.all, keys.objects.all, keys.files.all, chatKeys.chats()],
    },
  });
}

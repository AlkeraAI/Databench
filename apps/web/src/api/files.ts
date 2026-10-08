// Files: the drive, a folder's listing, and the facets the browser reads.
//
// Every read goes through the generated client, so a route or schema change
// lands here as a type error rather than as a wrong screen. Two wire details
// shape this module:
//
//  * The listing routes declare every filter chip, `orderBy` and `marker` in
//    the OpenAPI document, so they ride the typed client's own `query` object.
//    `toChildrenParams` still exists as the ONE place a caller's filters become
//    wire spellings: the record it returns is both the query key and the
//    request's query, so the key a page caches under and the string it sent can
//    never disagree.
//  * Nothing here polls on a timer. A `file_node.changed` / `file_operation.changed`
//    frame invalidates these keys through `events/eventMap.ts`; `streamDown` is
//    the fallback a caller passes when the stream is known to be down.

import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type QueryKey,
} from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { chatKeys } from "@/pages/workspace/chat/chatKeys";
import {
  FOLDER_CHILDREN_PAGE,
  OFFLINE_POLL_MS as WEB_OFFLINE_POLL_MS,
  QUERY_RETRY_ATTEMPTS,
} from "@/lib/limits";

import { api, apiBaseUrl, apiFetch, request } from "./client";
import { ApiError } from "./errors";
import { keys, type FilesListParams } from "./keys";

export type Item = components["schemas"]["Item"];
export type ChildrenPage = components["schemas"]["ChildrenPage"];
export type Drive = components["schemas"]["DriveWire"];
export type LeaseRow = components["schemas"]["LeaseRow"];
export type GrantList = components["schemas"]["GrantList"];
export type ShareCandidateList = components["schemas"]["ShareCandidateList"];
export type Operation = components["schemas"]["OperationWire"];
export type TrashPage = components["schemas"]["TrashPage"];
export type VersionList = components["schemas"]["VersionList"];

/**
 * Whether a node IS something else — a chat, a saved query, a report — rather
 * than the files it happens to keep.
 *
 * The server says so by hanging an `object` facet on the node, and it hangs it
 * on a folder as readily as on an `object` kind: a chat arrives as a
 * `.alkerachat` FOLDER carrying the facet. So the facet, never the kind, is
 * what says a node is atomic — it opens as the thing it is, it is not walked
 * into, and nothing may be filed inside it.
 */
export function isObjectBacked(item: Item): boolean {
  return item.object != null;
}

/** How often a hook re-reads while the caller says the event stream is down.
 *  The stream is the real refresh path; this only stops a disconnected tab from
 *  going permanently stale. */
export const OFFLINE_POLL_MS = WEB_OFFLINE_POLL_MS;

/** One marker page. The server's own default is 100; the browser scrolls in
 *  500s so a 20,000-row prefetch is 40 requests, not 200. */
export const CHILDREN_PAGE = FOLDER_CHILDREN_PAGE;

/** Every chip the listing surfaces offer, in the spelling the caller thinks in.
 *  `toChildrenParams` turns it into the wire spelling. */
export interface ListFilters {
  kind?: "file" | "folder" | "symlink" | "object";
  objectType?: string;
  mimeClass?: string;
  /** A principal id, or the literal `"me"` the server resolves to the caller. */
  owner?: string;
  modifiedAfter?: string;
  modifiedBefore?: string;
  sizeMin?: number;
  sizeMax?: number;
  nameFlag?: "windows_safe" | "macos_safe" | "display_warning";
  starred?: boolean;
  shared?: boolean;
  leased?: boolean;
  trashed?: boolean;
}

export type OrderField = "name" | "size" | "mtime" | "kind";
export type OrderDirection = "asc" | "desc";

export interface OrderBy {
  field: OrderField;
  direction?: OrderDirection;
}

const FILTER_WIRE: Readonly<Record<keyof ListFilters, string>> = {
  kind: "kind",
  objectType: "objectType",
  mimeClass: "mimeClass",
  owner: "owner",
  modifiedAfter: "modifiedAfter",
  modifiedBefore: "modifiedBefore",
  sizeMin: "sizeMin",
  sizeMax: "sizeMax",
  nameFlag: "nameFlag",
  starred: "starred",
  shared: "shared",
  leased: "leased",
  trashed: "trashed",
};

/**
 * The one place a listing's filters and order become wire parameters.
 *
 * Every listing read — a folder, search, and the saved filters behind Recent,
 * Starred and Shared with me — goes through this, so the key a hook caches
 * under and the query string it sends can never disagree. An unset filter is
 * omitted entirely (the server refuses a filter it cannot represent), and a
 * `false` chip is still sent, because "not starred" is a real filter.
 */
export function toChildrenParams(filters: ListFilters = {}, orderBy?: OrderBy): FilesListParams {
  const out: Record<string, string> = {};
  for (const [name, wire] of Object.entries(FILTER_WIRE) as [keyof ListFilters, string][]) {
    const value = filters[name];
    if (value === undefined) continue;
    out[wire] = typeof value === "string" ? value : String(value);
  }
  if (orderBy) {
    out.orderBy = orderBy.direction ? `${orderBy.field} ${orderBy.direction}` : orderBy.field;
  }
  return out;
}

/** `refetchInterval` for a hook whose freshness normally rides the event stream. */
const pollWhenOffline = (streamDown: boolean): number | false =>
  streamDown ? OFFLINE_POLL_MS : false;

export interface FilesReadOptions {
  /** True while the server event stream is known to be down: only then does a
   *  read fall back to polling. */
  streamDown?: boolean;
  /** Whether the read runs at all. Default true. False is for a caller that is
   *  mounted in a shell with no Files behind it — the editor's webview — where
   *  a drive read would go nowhere. */
  enabled?: boolean;
  /** Treat a 404 as an answer rather than a flake: ask once, then settle.
   *
   *  "This node is gone" is a decision the server has made — a file deleted
   *  since a tab was last open, a link to something that was trashed — and it
   *  does not become true again by asking twice more. Under the client's
   *  default ladder each one costs three extra requests and two backoff waits
   *  before the caller can render "no longer available", which a workspace
   *  restoring a screenful of stored tabs pays once per tab. Other statuses
   *  keep the ladder, because a 500 IS a flake. */
  settled404?: boolean;
}

/** The read's retry rule: a 404 settles at once when the caller asked for it,
 *  a 401 and a 403 never retry (they are answers too — asking three more times
 *  does not make a node the caller's, it only spends four requests and two
 *  backoff waits before the page can say so), everything else keeps the
 *  client's default ladder. */
function retryUnlessGone(settled404: boolean) {
  return (failureCount: number, error: unknown): boolean => {
    if (error instanceof ApiError) {
      if (error.status === 401 || error.status === 403) return false;
      if (settled404 && error.status === 404) return false;
    }
    return failureCount < QUERY_RETRY_ATTEMPTS;
  };
}

/** The caller's one org drive. */
export function useDrive(options: FilesReadOptions = {}) {
  return useQuery<Drive>({
    queryKey: keys.files.drive,
    queryFn: () => request(api.GET("/api/v1/files/drives", {})),
    enabled: options.enabled ?? true,
    refetchInterval: pollWhenOffline(options.streamDown ?? false),
  });
}

/** One node, by id. Disabled until both ids are known, so a page that has not
 *  routed yet holds its own cache entry instead of colliding with a real one. */
export function useItem(
  driveId: string | undefined,
  itemId: string | undefined,
  options: FilesReadOptions = {},
) {
  return useQuery<Item>(itemQuery(driveId, itemId, options));
}

/** The one read behind {@link useItem}, shared by every caller that reads
 *  several nodes at once so they land in the same cache entries. */
function itemQuery(
  driveId: string | undefined,
  itemId: string | undefined,
  options: FilesReadOptions = {},
) {
  return {
    queryKey: keys.files.item(itemId),
    enabled: (options.enabled ?? true) && Boolean(driveId) && Boolean(itemId),
    refetchInterval: pollWhenOffline(options.streamDown ?? false),
    retry: retryUnlessGone(options.settled404 ?? true),
    queryFn: (): Promise<Item> =>
      request(
        api.GET("/api/v1/files/drives/{drive_id}/items/{item_id}", {
          params: { path: { drive_id: driveId ?? "", item_id: itemId ?? "" } },
        }),
      ),
  };
}

export interface ChildrenOptions extends FilesReadOptions {
  filters?: ListFilters;
  orderBy?: OrderBy;
  limit?: number;
}

/**
 * A folder's children, one keyset page at a time.
 *
 * The cursor is the server's opaque `nextMarker`; a page that comes back
 * without one is the last page, and `getNextPageParam` returns undefined so the
 * caller's "load more" disappears rather than re-reading the final page for
 * ever.
 */
export function useChildren(
  driveId: string | undefined,
  parentId: string | undefined,
  options: ChildrenOptions = {},
) {
  const params = toChildrenParams(options.filters, options.orderBy);
  const limit = options.limit ?? CHILDREN_PAGE;
  return useInfiniteQuery<
    ChildrenPage,
    Error,
    ChildrenPage[],
    readonly unknown[],
    string | undefined
  >({
    queryKey: keys.files.children(driveId, parentId, params, limit),
    enabled: Boolean(driveId) && Boolean(parentId),
    initialPageParam: undefined,
    getNextPageParam: (last) => last.nextMarker ?? undefined,
    select: (data) => data.pages,
    refetchInterval: pollWhenOffline(options.streamDown ?? false),
    queryFn: ({ pageParam }) =>
      request(
        api.GET("/api/v1/files/drives/{drive_id}/items/{item_id}/children", {
          params: {
            path: { drive_id: driveId ?? "", item_id: parentId ?? "" },
            query: { limit, ...params, ...(pageParam ? { marker: pageParam } : {}) },
          },
        }),
      ),
  });
}

/** Every row loaded so far, flattened — what a treegrid renders. */
export function flattenChildren(pages: ChildrenPage[] | undefined): Item[] {
  return (pages ?? []).flatMap((page) => page.value);
}

/** A feed: Recent, Starred and Shared with me are drive-scoped
 *  routes of their own, not children of some node.
 *
 *  Reading them as a filtered child listing is what made the three rail places
 *  unreachable: a child listing is addressed by a node id, the drive names no
 *  node for "recent", so the place could never resolve one and stayed disabled
 *  forever. A feed needs the drive and nothing else. Each is one key, so the
 *  event map refreshes them with the folder. */
type FeedPath =
  | "/api/v1/files/drives/{drive_id}/recent"
  | "/api/v1/files/drives/{drive_id}/starred"
  | "/api/v1/files/drives/{drive_id}/sharedWithMe";

function feed(
  queryKey: readonly unknown[],
  path: FeedPath,
  driveId: string | undefined,
  options: FilesReadOptions,
) {
  return {
    queryKey,
    enabled: Boolean(driveId),
    refetchInterval: pollWhenOffline(options.streamDown ?? false),
    queryFn: (): Promise<ChildrenPage> =>
      request(
        api.GET(path, {
          params: { path: { drive_id: driveId ?? "" } },
        }),
      ) as Promise<ChildrenPage>,
  };
}

export function useRecent(driveId: string | undefined, options: FilesReadOptions = {}) {
  return useQuery<ChildrenPage>(
    feed(keys.files.recent, "/api/v1/files/drives/{drive_id}/recent", driveId, options),
  );
}

export function useStarred(driveId: string | undefined, options: FilesReadOptions = {}) {
  return useQuery<ChildrenPage>(
    feed(keys.files.starred, "/api/v1/files/drives/{drive_id}/starred", driveId, options),
  );
}

export function useSharedWithMe(driveId: string | undefined, options: FilesReadOptions = {}) {
  return useQuery<ChildrenPage>(
    feed(keys.files.sharedWithMe, "/api/v1/files/drives/{drive_id}/sharedWithMe", driveId, options),
  );
}

/** This drive's trashed roots, one marker page at a time. */
export function useTrash(driveId: string | undefined, options: FilesReadOptions = {}) {
  return useInfiniteQuery<TrashPage, Error, TrashPage[], readonly unknown[], string | undefined>({
    queryKey: keys.files.trash(driveId),
    enabled: Boolean(driveId),
    initialPageParam: undefined,
    getNextPageParam: (last) => last.nextMarker ?? undefined,
    select: (data) => data.pages,
    refetchInterval: pollWhenOffline(options.streamDown ?? false),
    queryFn: ({ pageParam }) =>
      request(
        api.GET("/api/v1/files/drives/{drive_id}/trash", {
          params: { path: { drive_id: driveId ?? "" }, query: { marker: pageParam ?? null } },
        }),
      ),
  });
}

/** One node's versions, oldest first. */
export function useVersions(
  driveId: string | undefined,
  nodeId: string | undefined,
  options: FilesReadOptions = {},
) {
  return useQuery<VersionList>({
    queryKey: keys.files.versions(nodeId),
    enabled: Boolean(driveId) && Boolean(nodeId),
    refetchInterval: pollWhenOffline(options.streamDown ?? false),
    queryFn: () =>
      request(
        api.GET("/api/v1/files/drives/{drive_id}/items/{node_id}/versions", {
          params: { path: { drive_id: driveId ?? "", node_id: nodeId ?? "" } },
        }),
      ),
  });
}

/** Who can reach a node. `effective` folds in the grants inherited from every
 *  ancestor, which is what the sharing summary shows. */
export function usePermissions(
  driveId: string | undefined,
  nodeId: string | undefined,
  options: FilesReadOptions & { effective?: boolean } = {},
) {
  return useQuery<GrantList>({
    queryKey: keys.files.permissions(nodeId, options.effective ?? false),
    enabled: Boolean(driveId) && Boolean(nodeId),
    refetchInterval: pollWhenOffline(options.streamDown ?? false),
    queryFn: () =>
      request(
        api.GET("/api/v1/files/drives/{drive_id}/items/{item_id}/permissions", {
          params: {
            path: { drive_id: driveId ?? "", item_id: nodeId ?? "" },
            query: { effective: options.effective ?? false },
          },
        }),
      ),
  });
}

/** The org's people and teams a share of this node may name, matching `query`.
 *
 *  Decided server-side as a share of the node, so a member who may share their
 *  own file can search the org's directory without being an org admin, and a
 *  caller who may not share gets the refusal rather than an empty list. A blank
 *  query asks nothing. */
export function useShareCandidates(
  driveId: string | undefined,
  nodeId: string | undefined,
  query: string,
  enabled = true,
) {
  const text = query.trim();
  return useQuery<ShareCandidateList>({
    queryKey: keys.files.shareCandidates(nodeId, text),
    enabled: enabled && Boolean(driveId) && Boolean(nodeId) && text !== "",
    // The previous answer stays on screen while the next keystroke's is out,
    // so the list narrows instead of flashing empty.
    placeholderData: keepPreviousData,
    // A refusal is an answer, not a blip: retrying it only delays saying so.
    retry: false,
    queryFn: () =>
      request(
        api.GET("/api/v1/files/drives/{drive_id}/items/{item_id}/share-candidates", {
          params: {
            path: { drive_id: driveId ?? "", item_id: nodeId ?? "" },
            query: { q: text },
          },
        }),
      ),
  });
}

/** My mounts across the drive. `mine=true` is the only question the server
 *  answers today, so "My leases" and the lease badges read one key. */
export function useLeases(driveId: string | undefined, options: FilesReadOptions = {}) {
  return useQuery<LeaseRow[]>({
    queryKey: keys.files.leases(driveId),
    enabled: Boolean(driveId),
    refetchInterval: pollWhenOffline(options.streamDown ?? false),
    queryFn: () =>
      request(
        api.GET("/api/v1/files/drives/{drive_id}/leases", {
          params: { path: { drive_id: driveId ?? "" }, query: { mine: true } },
        }),
      ),
  });
}

/** One operation's progress. Observable from a second session while the first
 *  is still running the work; it refreshes on `file_operation.changed` and only
 *  polls when the stream is down. */
export function useOperation(
  driveId: string | undefined,
  operationId: string | undefined,
  options: FilesReadOptions = {},
) {
  return useQuery<Operation>({
    queryKey: keys.files.operation(operationId),
    enabled: Boolean(driveId) && Boolean(operationId),
    refetchInterval: pollWhenOffline(options.streamDown ?? false),
    queryFn: () =>
      request(
        api.GET("/api/v1/files/drives/{drive_id}/operations/{operation_id}", {
          params: { path: { drive_id: driveId ?? "", operation_id: operationId ?? "" } },
        }),
      ),
  });
}

/** The named folders a member's own things are filed in — their home, their
 *  chats, their chat templates. */
export type Places = components["schemas"]["PlacesRead"];

/** The places a client may name. The server refuses anything else rather than
 *  answering a `null` for ever, so the union is the vocabulary, not a hint. */
export type PlaceName = "chats" | "chatTemplates";

/**
 * Where this caller's chats and chat templates are filed.
 *
 * A pure read: a folder nobody has needed yet answers `null`, so drawing a page
 * never puts a folder in somebody's drive. The ids come from here and are never
 * derived from a folder NAME — a member may rename or move theirs, and a
 * stranger may have put a folder of that name somewhere the client would find
 * it first.
 */
export function usePlaces(driveId: string | undefined, options: FilesReadOptions = {}) {
  return useQuery<Places>({
    queryKey: keys.files.places(driveId),
    enabled: (options.enabled ?? true) && Boolean(driveId),
    refetchInterval: pollWhenOffline(options.streamDown ?? false),
    queryFn: () =>
      request(
        api.GET("/api/v1/files/drives/{drive_id}/places", {
          params: { path: { drive_id: driveId ?? "" } },
        }),
      ),
  });
}

// ---------------------------------------------------------------------------
// mutations
// ---------------------------------------------------------------------------
//
// Three wire rules shape every hook below, and they are why the writes share
// helpers rather than each spelling their own headers:
//
//  * **`Idempotency-Key` on every non-GET** (the server answers 428 without
//    one). The key is minted per *attempt* and reused when react-query re-runs
//    `mutationFn` for a retry, so a retried write replays the stored answer
//    instead of performing the write a second time.
//  * **`If-Match` on every PATCH / PUT / DELETE** — the caller says which
//    version it believes it is changing. A stale etag is a 412, which is what
//    the optimistic hooks roll their patch back on.
//  * **No hand-wired invalidation.** `meta.invalidates` names the Files key
//    prefix and `createQueryClient`'s policy does the refresh (CLAUDE.md); the
//    optimistic `setQueryData` in `onMutate` is a pre-answer, not a substitute.

/** How the server should react when a write lands on a taken name. */
export type ConflictBehavior = "fail" | "rename";

/**
 * What a PATCH on a node answers: the changed node (200), or the operation the
 * server queued instead (202).
 *
 * A rename and a small move come straight back as the node. A move the server
 * decides to do in the background — a folder with a large subtree under it —
 * answers 202 with the operation instead, and there is no node to show yet, so
 * the caller has to be able to tell the two apart rather than read `name` off
 * something that has none.
 */
export type ItemOrOperation = Item | Operation;

/**
 * Which of the two came back.
 *
 * `ino` is the discriminator because every node has one and an operation never
 * does; the operation's own `kind` is a free string ("move", "copy"), so it
 * cannot be told apart from a node's `kind` by value.
 */
export function isOperation(result: ItemOrOperation): result is Operation {
  return !("ino" in result);
}

/** Every Files key starts with this prefix, so one entry refreshes the whole
 *  Files surface — the item, its folder's pages, trash, leases, permissions —
 *  and leaves the rest of the portal's cache alone. */
const FILES_INVALIDATES = [keys.files.all] as const;

/** The two disjoint cache entries a chat list lives in.
 *
 * `keys.chats.all` is `["chats"]`, a prefix of both the open chat's row and the
 * portal rail's list; `chatKeys.chats()` is `["ide","chat","chats"]`, the single
 * entry the chat header, the crumb trail and Chat home read. Neither prefixes
 * the other, so a mutation that moves a chat row has to name both or half the
 * portal goes on rendering what the server already dropped. */
const CHAT_LISTS = [keys.chats.all, chatKeys.chats()] as const;

/** What a Files write that can MOVE a chat row refreshes on top of the Files
 * surface.
 *
 * A chat's row carries its folder: the server derives `files_node_id` and
 * `files_node_trashed` from the node, so trashing a chat's folder — or restoring
 * it — rewrites the chat row too. `["files"]` never prefixes `["chats"]`, and no
 * server frame names the chat family for a Files change, so a chat-keyed read
 * that is not named here keeps the folder state the drive just left behind: the
 * rail goes on offering Share, Copy and Save as template on a trashed node.
 *
 * Undo carries it for the same reason: an undo is the inverse of a trash (which
 * is what Cmd+Z after a delete runs), of a duplicate, and of a rename, so it
 * moves the row exactly as the write it inverts did — in both directions, since
 * a redo is the undo of the undo.
 *
 * Only those carry it. A write INSIDE a chat's folder never moves the chat row,
 * and a machine saving emits one of those per file per save. */
const FILES_CHAT_ROW_INVALIDATES = [keys.files.all, ...CHAT_LISTS] as const;

/** What every node-scoped mutation carries: which node, at which version. */
export interface FilesWrite {
  driveId: string;
  itemId: string;
  /** The item's `etag`, sent as `If-Match`. */
  etag: string;
  /** A fenced write's `X-Alkera-Lease-Epoch` / `X-Alkera-Lease-Instance`. */
  headers?: Record<string, string>;
}

/** One attempt's `Idempotency-Key`, remembered against the variables object it
 *  was minted for. A `WeakMap` and not a field, so the memo can never leak into
 *  a request body and it dies with the attempt. */
const attemptKeys = new WeakMap<object, string>();

function mintKey(): string {
  const webCrypto = globalThis.crypto;
  if (webCrypto && typeof webCrypto.randomUUID === "function") return webCrypto.randomUUID();
  // A context without `randomUUID` still needs a key the server can store an
  // answer under; it only has to be unique per attempt, never unguessable.
  return `alk-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

/** What {@link withIdempotency} reads off an attempt. */
export interface Attempt {
  /** The version the caller believes it is changing, sent as `If-Match`. */
  etag?: string;
  headers?: Record<string, string>;
}

/**
 * The headers one write attempt sends.
 *
 * `init` is the mutation's own variables object: its identity IS the attempt,
 * so react-query calling `mutationFn` again for a retry hands the same object
 * back and gets the same `Idempotency-Key`, while a fresh
 * `mutate()` builds fresh variables and therefore mints a fresh key.
 */
export function withIdempotency(init: Attempt): Record<string, string> {
  let key = attemptKeys.get(init);
  if (key === undefined) {
    key = mintKey();
    attemptKeys.set(init, key);
  }
  const out: Record<string, string> = { ...(init.headers ?? {}), "Idempotency-Key": key };
  if (init.etag !== undefined) out["If-Match"] = init.etag;
  return out;
}

/** The infinite-query cache shape behind {@link useChildren}: the hook's
 *  `select` flattens the pages for its caller, but the CACHE still holds
 *  react-query's page envelope, which is what an optimistic patch must write. */
interface ChildrenCache {
  pages: ChildrenPage[];
  pageParams: unknown[];
}

/** A rolled-back optimistic patch: the exact cache entries as they were. */
type Snapshot = [QueryKey, unknown][];

/**
 * Apply `apply` to one row wherever it is cached, returning what was there
 * before. `apply` returning null drops the row — a move out of the folder, a
 * trash — which is what makes the browser react at the instant of the click.
 */
function patchCachedItem(
  qc: QueryClient,
  itemId: string,
  apply: (row: Item) => Item | null,
): Snapshot {
  const snapshot: Snapshot = [];

  const itemKey = keys.files.item(itemId);
  snapshot.push([itemKey, qc.getQueryData(itemKey)]);
  qc.setQueryData<Item>(itemKey, (old) => (old ? (apply(old) ?? old) : old));

  for (const family of [keys.files.childrenAll, keys.files.searchAll]) {
    for (const entry of qc.getQueriesData<ChildrenCache>({ queryKey: family })) {
      snapshot.push([entry[0], entry[1]]);
    }
    qc.setQueriesData<ChildrenCache>({ queryKey: family }, (old) => {
      if (!old?.pages) return old;
      return {
        ...old,
        pages: old.pages.map((page) => ({
          ...page,
          value: page.value.flatMap((row) => {
            if (row.id !== itemId) return [row];
            const next = apply(row);
            return next === null ? [] : [next];
          }),
        })),
      };
    });
  }
  return snapshot;
}

/** Put every snapshotted entry back, exactly as it was. */
function restoreSnapshot(qc: QueryClient, snapshot: Snapshot | undefined): void {
  for (const entry of snapshot ?? []) qc.setQueryData(entry[0], entry[1]);
}

/**
 * The shared optimistic wiring: patch on mutate, put it back on ANY failure.
 *
 * 412 is the one this exists for — a stale etag means the server refused and
 * the row must go back to the version the server still holds — but a 409 or a
 * dropped connection leaves an equally wrong screen, so every error rolls back
 * and the thrown {@link ApiError} carries the server's `code` for the page to
 * render inline.
 */
function optimistic<V extends { itemId: string }>(
  qc: QueryClient,
  apply: (vars: V) => (row: Item) => Item | null,
): {
  onMutate: (vars: V) => Snapshot;
  onError: (error: ApiError, vars: V, snapshot: Snapshot | undefined) => void;
} {
  return {
    onMutate: (vars: V): Snapshot => patchCachedItem(qc, vars.itemId, apply(vars)),
    onError: (_error: ApiError, _vars: V, snapshot: Snapshot | undefined): void =>
      restoreSnapshot(qc, snapshot),
  };
}

export interface RenameVars extends FilesWrite {
  name: string;
  conflictBehavior?: ConflictBehavior;
}

/** Rename in place. The new name is on screen before the request leaves. */
export function useRenameItem() {
  const qc = useQueryClient();
  return useMutation<ItemOrOperation, ApiError, RenameVars, Snapshot>({
    meta: { invalidates: FILES_INVALIDATES },
    ...optimistic<RenameVars>(qc, (vars) => (row) => ({
      ...row,
      name: vars.name,
      nameDisplay: vars.name,
    })),
    mutationFn: (vars) =>
      request(
        api.PATCH("/api/v1/files/drives/{drive_id}/items/{item_id}", {
          params: {
            path: { drive_id: vars.driveId, item_id: vars.itemId },
            query: { conflict_behavior: vars.conflictBehavior ?? "fail" },
          },
          headers: withIdempotency(vars),
          body: { name: vars.name },
        }),
        "could not rename",
      ),
  });
}

export interface MoveVars extends FilesWrite {
  parentId: string;
  conflictBehavior?: ConflictBehavior;
}

/** Move to another folder. The row leaves the folder it was listed in at once;
 *  the destination's pages come back through the invalidation policy.
 *
 *  A move the server queues answers 202 with the operation instead of the node —
 *  {@link isOperation} says which came back, and the caller follows it with
 *  {@link useOperation} rather than reading a name off something that has none. */
export function useMoveItem() {
  const qc = useQueryClient();
  return useMutation<ItemOrOperation, ApiError, MoveVars, Snapshot>({
    meta: { invalidates: FILES_INVALIDATES },
    ...optimistic<MoveVars>(qc, (vars) => (row) =>
      row.parentId === vars.parentId ? row : null,
    ),
    mutationFn: (vars) =>
      request(
        api.PATCH("/api/v1/files/drives/{drive_id}/items/{item_id}", {
          params: {
            path: { drive_id: vars.driveId, item_id: vars.itemId },
            query: { conflict_behavior: vars.conflictBehavior ?? "fail" },
          },
          headers: withIdempotency(vars),
          body: { parentId: vars.parentId },
        }),
        "could not move",
      ),
  });
}

export interface CopyVars extends Attempt {
  driveId: string;
  /** The node being copied. */
  itemId: string;
  /** The folder the copy lands in. */
  parentId: string;
  /** A new name for the copy; omit to keep the source's. */
  name?: string;
  conflictBehavior?: ConflictBehavior | "replace";
}

/**
 * Copy a node into another folder.
 *
 * Always an operation: the route answers 202 whether it is one file or forty
 * thousand, so a caller writes one progress path instead of two. Nothing is
 * patched optimistically — the copy has no id until the server makes one — and
 * no `If-Match` rides along, because a copy changes no version the caller holds.
 */
export function useCopyItem() {
  return useMutation<Operation, ApiError, CopyVars>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/items/{item_id}/copy", {
          params: { path: { drive_id: vars.driveId, item_id: vars.itemId } },
          headers: withIdempotency(vars),
          body: {
            parentId: vars.parentId,
            name: vars.name ?? null,
            conflictBehavior: vars.conflictBehavior ?? "rename",
          },
        }),
        "could not copy",
      ),
  });
}

export type Duplicated = components["schemas"]["DuplicateResult"];

export interface DuplicateVars extends Attempt {
  driveId: string;
  /** The node being copied: a file, a folder, a chat, a saved object. */
  itemId: string;
  /** Where the copy lands; omit for the reader's own drive (a chat goes to
   *  their Chats folder, everything else to their home). */
  destinationId?: string;
  /** A new name for the copy; omit to keep the source's. */
  name?: string;
}

/**
 * Copy a node into the reader's own drive, or a folder they name.
 *
 * Unlike {@link useCopyItem} this answers the copy itself, run to its end:
 * the new node and, for a chat or a saved object, the new object's id — the
 * chat a reader opens next. Nothing is patched optimistically; the copy has
 * no id until the server makes one.
 */
export function useDuplicateItem() {
  return useMutation<Duplicated, ApiError, DuplicateVars>({
    // A copied chat is also a new row in BOTH chat lists — the reader is
    // navigated straight into the copy, and the entry its header, its crumbs
    // and Chat home read is not the one the rail reads.
    meta: { invalidates: [keys.files.all, ...CHAT_LISTS] },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/items/{item_id}/duplicate", {
          params: { path: { drive_id: vars.driveId, item_id: vars.itemId } },
          headers: withIdempotency(vars),
          body: {
            destinationId: vars.destinationId ?? null,
            name: vars.name ?? null,
          },
        }),
        "could not copy",
      ),
  });
}

export interface TrashVars extends FilesWrite {
  /** True purges instead of trashing. Not undoable, so it has to be asked for. */
  permanent?: boolean;
}

/**
 * Move to trash (or purge). The row disappears at the click and comes back if
 * the server refuses.
 *
 * The two branches answer differently and a caller needs both. Trashing is
 * undoable, so the route runs it AS an operation and answers 200 with that
 * operation — the handle `POST …/operations/{id}/undo` accepts, and the only
 * thing that makes Cmd+Z after a delete mean anything. The purge has no
 * inverse and answers 204, which arrives here as `null` rather than as an id
 * the browser would have to invent.
 */
export function useTrashItem() {
  const qc = useQueryClient();
  return useMutation<Operation | null, ApiError, TrashVars, Snapshot>({
    meta: { invalidates: FILES_CHAT_ROW_INVALIDATES },
    ...optimistic<TrashVars>(qc, () => () => null),
    mutationFn: async (vars) => {
      const answered = await request<Operation | undefined>(
        api.DELETE("/api/v1/files/drives/{drive_id}/items/{item_id}", {
          params: {
            path: { drive_id: vars.driveId, item_id: vars.itemId },
            query: { permanent: vars.permanent ?? false },
          },
          headers: withIdempotency(vars),
        }),
        "could not move to trash",
      );
      // A 204 has no body, so the client hands back nothing; normalising it to
      // `null` here keeps "there is no inverse" a value a caller can branch on.
      return answered ?? null;
    },
  });
}

/**
 * A node's etag as the integer counter the batch route's per-item `ifMatch`
 * carries, or `null` when it is not one.
 *
 * The header and the batch field are the same counter spelled two ways: a
 * single-row write sends the etag as `If-Match` and the server parses it back
 * to an integer, while a batch item declares the integer itself. Parsed here
 * the way the server parses the header — a weak validator and the quotes are
 * both allowed — so the two spellings cannot drift. A value that is not a
 * counter yields `null` rather than `NaN`, so a caller can fall back to the
 * route that takes the header rather than sending a body the server refuses.
 */
export function etagVersion(etag: string): number | null {
  let text = etag.trim();
  if (text.startsWith("W/")) text = text.slice(2);
  text = text.replace(/^"|"$/g, "");
  if (!/^\d+$/.test(text)) return null;
  return Number(text);
}

/** One row of a batch, as the `bulk` route spells it. */
export interface BulkTrashRow {
  readonly itemId: string;
  /** The version the row was read at, fenced per item the way a single-row
   *  write fences with `If-Match`. */
  readonly etag: string;
}

export interface BulkTrashVars extends Attempt {
  driveId: string;
  /** At most {@link FILES_BULK_BATCH_ITEMS}; the server refuses a longer body. */
  items: readonly BulkTrashRow[];
}

/** One item's own answer, carrying the status and body that item would have got
 *  as a request of its own — which is how a caller reuses one error path for the
 *  single-row route and the batch. */
export interface BulkItemResult {
  readonly id: string;
  readonly status: number;
  readonly body?: { code?: string; message?: string } | null;
}

/** What one batch answers with: a queued operation past the server's inline
 *  ceiling, or a row-by-row report when it ran inline. An item that refuses
 *  rolls back only itself, so an inline batch is a partial success by
 *  construction and the rows are the only record of which half is which. */
export type BulkTrashResult = Operation | { responses: BulkItemResult[] };

/**
 * Trash a batch of rows in one request.
 *
 * The single-row route is still the right one for a short selection — it mints
 * an operation per row, so undoing one leaves the others trashed. This is for
 * the long selection `Select all` can now arm, where one request per row is a
 * request storm and a rate-limit slot each; past the server's inline ceiling the
 * batch comes back as a queued operation carrying progress and a cancel.
 */
export function useBulkTrash() {
  return useMutation<BulkTrashResult, ApiError, BulkTrashVars>({
    meta: { invalidates: FILES_CHAT_ROW_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/bulk", {
          params: { path: { drive_id: vars.driveId } },
          body: {
            items: vars.items.map((row, at) => ({
              // The caller's own correlation handle, never an id this server
              // issued: the answers come back carrying it, in order.
              id: `t${at}`,
              op: "trash" as const,
              itemId: row.itemId,
              ifMatch: etagVersion(row.etag) ?? undefined,
              // A trash lands in the trash's own namespace, where the server
              // already suffixes a taken name; `fail` is the batch default and
              // the one that never renames a row behind the reader's back.
              conflictBehavior: "fail" as const,
            })),
          },
          headers: withIdempotency(vars),
        }) as Promise<{ data?: BulkTrashResult; error?: unknown; response: Response }>,
        "could not move to trash",
      ),
  });
}

export interface RestoreVars extends Attempt {
  driveId: string;
  /** The trash op to undo — a deletion, not a node. */
  opId: string;
  /** Where it should land; omit for its original place. */
  parentId?: string;
}

/** Bring one deletion back, at its old place or a named one. */
export function useRestoreTrash() {
  return useMutation<unknown, ApiError, RestoreVars>({
    meta: { invalidates: FILES_CHAT_ROW_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/trash/{op_id}/restore", {
          params: { path: { drive_id: vars.driveId, op_id: vars.opId } },
          headers: withIdempotency(vars),
          body: { parent_id: vars.parentId ?? null },
        }),
        "could not restore",
      ),
  });
}

export interface EmptyTrashVars extends Attempt {
  driveId: string;
}

/** What one sweep of the trash did: what it purged, and what it left behind
 *  because the caller may not delete it. */
export type TrashEmptyResult = components["schemas"]["TrashEmptyResult"];

/**
 * Purge every trashed root this caller may delete.
 *
 * The server decides per deletion rather than per drive, so a caller who may
 * not delete one of the roots does not get it purged by asking for all of them
 * and a held subtree still refuses — which is why the answer is not "the trash
 * is now empty" and the view re-reads instead of assuming. The counts come back
 * typed for the same reason: the notice the page writes is the only place a
 * person learns that something stayed.
 *
 * Restoring stays op-addressed: there is no node-addressed restore route,
 * because the trash lists *deletions*, so a caller holding only a node id finds
 * its trash row first and calls {@link useRestoreTrash} with the op id.
 */
export function useEmptyTrash() {
  return useMutation<TrashEmptyResult, ApiError, EmptyTrashVars>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/trash/empty", {
          params: { path: { drive_id: vars.driveId } },
          headers: withIdempotency(vars),
        }),
        "could not empty the trash",
      ),
  });
}

export interface CreateFolderVars extends Attempt {
  driveId: string;
  /** The folder the new node goes in. */
  parentId: string;
  name: string;
  kind?: "folder" | "symlink" | "special";
  symlinkTarget?: string;
  conflictBehavior?: ConflictBehavior;
}

/** One new folder (or symlink, or special node) under a folder. */
export function useCreateFolder() {
  return useMutation<Item, ApiError, CreateFolderVars>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/items/{item_id}/children", {
          params: { path: { drive_id: vars.driveId, item_id: vars.parentId } },
          headers: withIdempotency(vars),
          body: {
            name: vars.name,
            kind: vars.kind ?? "folder",
            symlinkTarget: vars.symlinkTarget ?? null,
            conflictBehavior: vars.conflictBehavior ?? "fail",
          },
        }),
        "could not create the folder",
      ),
  });
}

export interface CreateTreeVars extends Attempt {
  driveId: string;
  parentId: string;
  /** Slash-joined relative paths — the folder skeleton of a dropped directory. */
  paths: string[];
}

/** The folder skeleton of a dropped directory, in one call. */
export function useCreateTree() {
  return useMutation<Item[], ApiError, CreateTreeVars>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/items/{item_id}/tree", {
          params: { path: { drive_id: vars.driveId, item_id: vars.parentId } },
          headers: withIdempotency(vars),
          body: { paths: vars.paths },
        }),
        "could not create the folders",
      ),
  });
}

export interface EnsurePlacesVars {
  driveId: string;
  /** The places to make if they are not there yet. */
  places: readonly PlaceName[];
}

/**
 * Make the named places under the caller's home and answer their ids.
 *
 * A write, not a read, even though the route is a `GET`: a surface about to
 * file something in a member's `Chat Templates` folder needs the id of a folder
 * that may not exist yet, and asking for it in one request is what stops the
 * "read, notice the null, create, read again" dance.
 *
 * The answer seeds the places entry so the surface that navigates straight
 * after does not pay for a second read. The seed lands after the policy's
 * refetch, which is what makes it the freshest value rather than a race.
 */
export function useEnsurePlaces() {
  const qc = useQueryClient();
  return useMutation<Places, ApiError, EnsurePlacesVars>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.GET("/api/v1/files/drives/{drive_id}/places", {
          params: {
            path: { drive_id: vars.driveId },
            query: { ensure: vars.places.join(",") },
          },
        }),
        "could not open that folder",
      ),
    onSuccess: (places, vars) => {
      qc.setQueryData<Places>(keys.files.places(vars.driveId), places);
    },
  });
}

export interface StarVars extends FilesWrite {
  starred: boolean;
}

/** Set or clear the star. Optimistic, because a bookmark that lags the click
 *  reads as a broken button. */
export function useStar() {
  const qc = useQueryClient();
  return useMutation<Item, ApiError, StarVars, Snapshot>({
    meta: { invalidates: FILES_INVALIDATES },
    ...optimistic<StarVars>(qc, (vars) => (row) => ({ ...row, starred: vars.starred })),
    mutationFn: (vars) => {
      const options = {
        params: { path: { drive_id: vars.driveId, item_id: vars.itemId } },
        headers: withIdempotency(vars),
      };
      return request(
        vars.starred
          ? api.PUT("/api/v1/files/drives/{drive_id}/items/{item_id}/star", options)
          : api.DELETE("/api/v1/files/drives/{drive_id}/items/{item_id}/star", options),
        "could not star",
      );
    },
  });
}

export interface OperationVars extends Attempt {
  driveId: string;
  operationId: string;
}

/** Apply an operation's inverse as a NEW operation — what the undo toast does. */
export function useUndoOperation() {
  return useMutation<Operation, ApiError, OperationVars>({
    meta: { invalidates: FILES_CHAT_ROW_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/operations/{operation_id}/undo", {
          params: { path: { drive_id: vars.driveId, operation_id: vars.operationId } },
          headers: withIdempotency(vars),
        }),
        "could not undo",
      ),
  });
}

/** Ask an operation to stop. A running one stops at its next batch boundary, so
 *  the answer may still say `running` — the tray renders what came back. */
export function useCancelOperation() {
  return useMutation<Operation, ApiError, OperationVars>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/operations/{operation_id}/cancel", {
          params: { path: { drive_id: vars.driveId, operation_id: vars.operationId } },
          headers: withIdempotency(vars),
        }),
        "could not cancel",
      ),
  });
}

export interface GrantVars extends FilesWrite {
  principal: { kind: string; id: string };
  role: string;
  expiresAt?: string;
}

/** Grant a role on one node to a principal of this org. */
export function useGrant() {
  return useMutation<unknown, ApiError, GrantVars>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/items/{item_id}/permissions", {
          params: { path: { drive_id: vars.driveId, item_id: vars.itemId } },
          headers: withIdempotency(vars),
          body: {
            principal: { kind: vars.principal.kind, id: vars.principal.id },
            role: vars.role,
            expires_at: vars.expiresAt ?? null,
          },
        }),
        "could not share",
      ),
  });
}

export interface RevokeVars extends FilesWrite {
  shareId: string;
}

/** Withdraw a direct grant. An inherited one is refused with the ancestor named
 *  (`files.inherited_grant`), which the page shows instead of a generic error. */
export function useRevoke() {
  return useMutation<unknown, ApiError, RevokeVars>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.DELETE("/api/v1/files/drives/{drive_id}/items/{item_id}/permissions/{share_id}", {
          params: {
            path: { drive_id: vars.driveId, item_id: vars.itemId, share_id: vars.shareId },
          },
          headers: withIdempotency(vars),
        }),
        "could not remove access",
      ),
  });
}

export interface RestoreVersionVars extends FilesWrite {
  /** The version to make the head again. */
  versionId: string;
}

/**
 * Put an older version back.
 *
 * A forward write, never a rewind: the server APPENDS the old bytes as a new
 * version rather than dropping the ones above them, so nothing a person was
 * looking at is lost by restoring and the restore is itself undoable.
 *
 * `["files"]` prefixes the node's own entry, its folder's pages and its version
 * list, so the row's size and modified time move with the history the dialog
 * re-reads — the three would otherwise disagree on screen until the next event
 * frame arrived.
 */
export function useRestoreVersion() {
  return useMutation<unknown, ApiError, RestoreVersionVars>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST(
          "/api/v1/files/drives/{drive_id}/items/{node_id}/versions/{version_id}/restore",
          {
            params: {
              path: {
                drive_id: vars.driveId,
                node_id: vars.itemId,
                version_id: vars.versionId,
              },
            },
            headers: withIdempotency(vars),
          },
        ),
        "could not restore that version",
      ),
  });
}

export interface ReleaseLeaseVars extends FilesWrite {
  epoch: number;
  instanceId: string;
}

/** Give a lease back. The holder's final snapshot and the release commit in one
 *  transaction, so "finishing sync…" ends exactly when this resolves. */
export function useReleaseLease() {
  return useMutation<unknown, ApiError, ReleaseLeaseVars>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/items/{item_id}/lease/release", {
          params: { path: { drive_id: vars.driveId, item_id: vars.itemId } },
          headers: withIdempotency(vars),
          body: { epoch: vars.epoch, instanceId: vars.instanceId, final: null },
        }),
        "could not release the lease",
      ),
  });
}

/** Ask the holder to hand a leased folder back. */
export function useRequestRelease() {
  return useMutation<LeaseRow, ApiError, FilesWrite>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/items/{item_id}/lease/request-release", {
          params: { path: { drive_id: vars.driveId, item_id: vars.itemId } },
          headers: withIdempotency(vars),
        }),
        "could not ask for the folder back",
      ),
  });
}

/** A manager taking a lease away. The holder is told and the lease lapses after
 *  one TTL, which is why the confirmation names the grace period. */
export interface ForceReleaseVars extends FilesWrite {
  /** Why a box's lease is being taken back. The server asks for one when the
   *  holder is a box (a chat's or a workspace's lease) and keeps it on record;
   *  sent only when given, so a server that asks for none sees the request it
   *  always saw. */
  reason?: string;
}

export function useForceRelease() {
  return useMutation<unknown, ApiError, ForceReleaseVars>({
    meta: { invalidates: FILES_INVALIDATES },
    mutationFn: (vars) =>
      request(
        api.POST("/api/v1/files/drives/{drive_id}/items/{item_id}/lease/force-release", {
          params: { path: { drive_id: vars.driveId, item_id: vars.itemId } },
          headers: withIdempotency(vars),
          ...(vars.reason ? { body: { reason: vars.reason } } : {}),
        }),
        "could not force the release",
      ),
  });
}

// --- Content grants ---------------------------------------------------------
//
// Bytes are served from a different origin than the app, and reaching them needs
// a grant: a short-lived URL the server mints for ONE node after deciding the
// caller may export it. Two kinds, and the difference matters:
//
//  * `file` — single use, minutes. What a preview fetches for an image, a CSV, a
//    text file. Spent by the read it was minted for, so every version buys its
//    own.
//  * `page` — a document that renders itself (a page, a PDF) together with the
//    files beside it that it names. What an `<iframe>` loads.
//
// The grant IS the authorization, so it is a bearer credential for as long as it
// lives: it never enters the URL bar, a link, a copyable field or telemetry.
// Minting is spelled by hand rather than through the generated client: it
// answers a fresh URL per call, caches nothing, and is not a read of any row.

export interface ContentGrant {
  /** Where the minted bytes stand against the disk of the machine holding the
   *  folder: `behind` means the URL serves the store's older copy, as of `asOf`. */
  contentState?: components["schemas"]["ContentGrantResponse"]["contentState"];
  asOf?: string | null;
  /** The URL the bytes are read from. Treat it as a credential. */
  url: string;
  expiresAt: string;
  kind: "file" | "page";
  /** The item etag the grant was minted against. */
  etag: string;
}

export interface MintGrantVars {
  driveId: string;
  itemId: string;
  kind: "file" | "page";
  disposition?: "inline" | "attachment";
}

/** The door a preview surface is handed so it can buy bytes without knowing the
 *  route. `useMintContentGrant().mutateAsync` fills it. */
export type MintContentGrant = (vars: MintGrantVars) => Promise<ContentGrant>;

/** Mint one grant for a node's bytes.
 *
 *  Invalidates nothing: a grant is a new short-lived URL, not a change to any
 *  row the browser holds, so a refresh after one would be waste on every
 *  preview. */
export function useMintContentGrant() {
  return useMutation<ContentGrant, ApiError, MintGrantVars>({
    meta: { invalidates: "none" },
    mutationFn: async (vars) => {
      const path = `/api/v1/files/drives/${encodeURIComponent(vars.driveId)}/items/${encodeURIComponent(vars.itemId)}/content-grants`;
      const response = await apiFetch(new URL(path, apiBaseUrl).toString(), {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ kind: vars.kind, disposition: vars.disposition ?? "inline" }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new ApiError(response.status, body, "could not open this file", response.headers);
      }
      return (await response.json()) as ContentGrant;
    },
  });
}

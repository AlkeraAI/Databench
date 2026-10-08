// What a server event means for the query cache.
//
// A frame on the event stream is thin: it names WHAT changed (type, entity, id,
// version, org) and never the change. This map turns each type into the cache
// slots that may now be stale, spelled through `api/keys.ts` so an event can
// only ever name a key a hook really uses. The client then refetches through
// the REST surface it is authorized for — the stream can never leak a field the
// reader is not entitled to.
//
// The map is typed exhaustive over the server's `RealtimeEventType` (generated
// into `@alkera/sdk` from the SSE route's response model) and a test pins the
// runtime key set against `openapi.json`, so a new server event that has no
// portal reaction fails a build, not a user. Curated, not derived: like the
// webview's surface-key map, each entry is the judgement of which reads a
// change can touch, erring towards one prefix too many (a wasted refetch) over
// one too few (a stale screen).

import { hashKey, type QueryClient, type QueryKey } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { chatKeys } from "@/pages/workspace/chat/chatKeys";

import { notGone } from "../gone";
import { keys } from "../keys";

import { MACHINE_RATE_NODE_REASONS, isMachineRate, machineRefresh, refreshForSave } from "./machineRefresh";

export type RealtimeEventType = components["schemas"]["RealtimeEventType"];
/**
 * The `data:` body of one `event: <type>` frame, exactly as the server spells it,
 * plus the few ids a frame may carry alongside the entity it names.
 *
 * They stay optional and stay IDS: a frame is a "something changed here" notice
 * the client answers by re-reading through the REST surface it is authorized
 * for, so widening it with a name, a path or a payload would be a leak. The
 * extras exist only to narrow which cache slot is re-read — an older server that
 * sends none costs a wider refetch and nothing else.
 */
export type RealtimeEventFrame = components["schemas"]["SseEventData"] & {
  /** The drive the changed node belongs to. */
  readonly drive_id?: string;
  /** The folder the changed node sits in — absent for a root, and absent from a
   *  server that predates it. */
  readonly parent_id?: string;
  /** Why the node changed, when the server has a word for it. */
  readonly reason?: string;
  /** The leased folder a lease frame is about — the root of the subtree whose
   *  in-flight plane moved, not the file inside it that moved. */
  readonly lease_node_id?: string;
} & {
  /** A lease frame whose batch touched more folders than it named one by one:
   *  every open folder under the lease may have moved. */
  readonly subtree?: boolean;
};

export type KeysFor = (frame: RealtimeEventFrame) => readonly QueryKey[];

// A node frame with a machine-rate reason (see `MACHINE_RATE_NODE_REASONS`):
// a leased folder's holder landing bytes it had been reporting as in flight, a
// write the holder's own copy then won, a batch of the holder's disk reaching
// the drive, a file both sides changed that the drive settled by keeping both,
// or a live document's write-back. An agent writing files emits them many times
// a second, and none can change which chats a reader may list, so a frame
// carrying one leaves the chat family out. A share grant, a rename, a move, a
// trash and a restore carry no reason and keep it.

export const EVENT_KEYS: Record<RealtimeEventType, KeysFor> = {
  // A run landed or changed: the history, the 30-day summary, the run itself, the live band,
  // the wiring status and the drift feed all read it.
  "gate_run.ingested": (f) => [
    keys.gate.runsAll,
    keys.gate.summary,
    keys.gate.run(f.entity_id),
    keys.gate.activityAll,
    keys.gate.status,
    keys.gate.driftAll,
  ],
  "gate_lease.changed": () => [keys.gate.activityAll, keys.gate.status],
  // The thin frame carries no team id, so the whole family refreshes; only the mounted team's
  // list is active, so the cost is one refetch.
  "team_connection.updated": () => [
    keys.teamConnections.all,
    keys.connections.me,
    keys.connectionInventory.me,
    keys.connectionInventory.org,
  ],
  "team_connection.probed": () => [
    keys.teamConnections.all,
    keys.connections.me,
    keys.connectionInventory.me,
    keys.connectionInventory.org,
  ],
  // A verification record moved (queued → running → settled | abandoned). The
  // record's own query is what the add dialog reads, and any list showing the
  // row whose check it is may now print a different badge.
  "connection.verification_changed": (f) => [
    keys.teamConnections.verification(f.entity_id),
    keys.teamConnections.all,
    keys.connections.me,
  ],
  // A credential's state moved on its own axis (an OAuth refresh refused, a
  // member signed out, a ciphertext that won't open) — no verification ran, and
  // the badge changed anyway.
  "connection.credential_changed": () => [keys.teamConnections.all, keys.connections.me],
  "billing.summary_changed": () => [keys.billing.summary, keys.me.credits],
  "org_billing.changed": () => [keys.org.billing, keys.orgAdmin.members],
  "kb_item.changed": () => [keys.kb.all],
  "user.email_verified": () => [keys.auth.me],
  // Somebody joined, left, or changed role. The team tree and the rosters are the
  // obvious readers; the rest are the reads whose ANSWER a membership decides and
  // which show nowhere else:
  //  - `/me/connections` returns the caller's own rows plus every team they belong
  //    to, so joining or leaving changes which connections a person may use;
  //  - `/auth/me` carries `admin_team_ids`, which a role change rewrites;
  //  - the org billing view and the storage dashboard both draw a per-member table;
  //  - a member's plan/tier/usage sits under its own key that the members prefix
  //    does not cover.
  "membership.changed": () => [
    keys.teams.all,
    keys.teams.membersAll,
    keys.teams.memberPlanAll,
    keys.orgAdmin.members,
    keys.dashboard.identity,
    keys.auth.me,
    keys.connections.me,
    keys.teamConnections.all,
    keys.org.billing,
    keys.org.storage,
  ],
  "invitation.changed": () => [keys.invitations.all],
  // A chat's metadata moved (its title, its machine binding, its last sequence): the rail
  // that lists chats and the header of the chat itself both read it. The transcript does NOT
  // come from here — it rides the doc-sync socket, which delivers the messages themselves.
  // The rail, the header and the crumb trail read the list through the chat pages' own
  // `chatKeys.chats()` entry, so the frame has to name it too — without it a rename made in
  // another tab (or by the agent) showed nowhere until the page was reopened.
  // The workspace list carries each reader's unread count, which a finished
  // turn or a collaborator's message moves, so it follows the same frame.
  "chat.updated": (f) => [keys.chats.all, keys.chats.one(f.entity_id), chatKeys.chats(), keys.workspaces.all],
  "artifact.updated": () => [keys.kb.all],
  // A workspace object was created, edited, or finished uploading its payload. The list and
  // the object's own page read it; the rows page hangs under the object key, so one prefix
  // covers the table and the receipt too.
  // A chat template is a workspace object too, and it is read on surfaces that sit
  // nowhere near the objects family — the templates browser and a template's own
  // page — so a save, a rename or a delete has to reach them by their own prefix.
  // A chat is a workspace object too, and this route is the ONLY way one is
  // renamed: the object write announces THIS frame and no `chat.updated`. The
  // frame says what changed and never what kind of thing it is, so the chat
  // entries are named unconditionally — an object change that was not a chat
  // costs one coalesced list read, and without them a rename made by a teammate
  // or in another tab of this account showed nowhere until a reload.
  // A workspace is a workspace object as well: made, renamed or ended by anyone
  // with access, and a teammate's new workspace or rename has to reach this
  // reader's rail without a reload. The prefix covers the open one too.
  "workspace_object.changed": (f) => [
    keys.workspaces.all,
    keys.objects.all,
    keys.objects.one(f.entity_id),
    keys.chatTemplates.all,
    keys.chatTemplates.one(f.entity_id),
    keys.chats.all,
    keys.chats.one(f.entity_id),
    chatKeys.chats(),
  ],
  // The org's machine changed state. The chat banner reads it, and a chat row carries the
  // machine's status, so the chat family refreshes with it — including the chat pages' own
  // list entry, which is what the rail draws each row's machine badge from. Without that
  // entry the badge kept the state the page was opened with until it was reopened.
  "compute_machine.changed": () => [keys.machines.current, keys.chats.all, chatKeys.chats()],
  // An org machine started, stopped or failed. The banner and every chat row that runs on it
  // read its state the same way they read the workspace machine's. The Machines pages, and
  // every workspace chip naming it, read its state and name too.
  "org_machine.changed": () => [
    keys.machines.current,
    keys.machines.org,
    keys.machines.workspaceAll,
    keys.chats.all,
    chatKeys.chats(),
  ],
  // A workspace's move to another machine advanced: the workspace reads where it runs, and so
  // does every chat in it. The workspace's machine read carries the move's step.
  "workspace.machine_move": (f) => [
    keys.workspaces.all,
    keys.objects.one(f.entity_id),
    keys.machines.workspace(f.entity_id),
    keys.machines.org,
    keys.machines.current,
    keys.chats.all,
    chatKeys.chats(),
  ],
  // A node was created, renamed, moved, starred, shared, leased or trashed. The
  // item's own key refreshes precisely; the listings refresh as narrowly as the
  // frame allows. When it names the folder the node sits in, only that folder's
  // listings re-read — a machine writing into an open chat folder emits one of
  // these per file per save, and the family prefix would re-fetch every folder
  // the browser holds each time. With no folder named (a root, or a server that
  // predates the field) the fallback is the wider refetch, never a stale row.
  // Sharing a node IS a node change and shows nowhere but the grant reads, so
  // they refresh with it; the lease facet rides the node, so a lease taken or
  // released lands here too.
  // A chat's node is one of these, and the chat list is cut by what the reader
  // may reach — so granting or revoking access to it changes WHICH chats the
  // other person may list. The reader's own mutation cannot refresh that: the
  // grant is written by somebody else's session, and this frame is all theirs
  // hears. The rail reads the list under the chat pages' own entry and the
  // header and crumbs read the portal family, so both are named — but only for
  // a frame that could have moved a grant, never for the machine-rate reasons,
  // whose flood would re-run the whole paged list enumeration on every seat in
  // the org several times a second.
  // A machine landing bytes it had been reporting as in flight changes the
  // node and the folder it sits in, and NOTHING else: not who may read it, not
  // what the reader starred, searched for, trashed or was shared, and not which
  // chats they may list. An agent writing a file emits one of these per save,
  // and the wide fan-out below — eight prefixes, two of them paged listings —
  // is what took an open chat page past the API's own rate limiter while its
  // reader sat still. So a machine-rate frame refreshes the two slots whose
  // answer really moved, and the rest are for the frames a PERSON caused. A
  // conflict frame adds the one list it moved: the drive's open conflicts.
  "file_node.changed": (f) => {
    const here =
      f.parent_id && f.drive_id
        ? keys.files.childrenOf(f.drive_id, f.parent_id)
        : keys.files.childrenAll;
    if (f.reason !== undefined && MACHINE_RATE_NODE_REASONS.has(f.reason)) {
      return [keys.files.item(f.entity_id), here];
    }
    return [
      keys.files.item(f.entity_id),
      here,
      keys.files.permissionsAll,
      keys.files.searchAll,
      keys.files.recent,
      keys.files.starred,
      keys.files.sharedWithMe,
      keys.files.trashAll,
      keys.files.leasesAll,
      keys.chats.all,
      chatKeys.chats(),
    ];
  },
  // The in-flight plane of a leased folder moved: a file the machine is writing
  // started, finished uploading, or was left on the box. One frame covers the
  // whole subtree — the plane is read back through the listing, which the node
  // frames already refresh — so this only has to refresh the leased folder's own
  // item (the facet the header reads rides it) and the lease feeds. The frame
  // names the leased node in its payload; an older server that names it only as
  // the entity is read the same way. A `subtree` frame (a batch that touched too
  // many folders to name) widens nothing here: the folders a reader has open
  // are `useFolderLiveness`'s to refresh, and the drive-wide listing prefix
  // would re-read every folder the browser ever held.
  "file_lease.changed": (f) => [
    keys.files.item(f.lease_node_id ?? f.entity_id),
    keys.files.leasesAll,
  ],
  // An operation's state or progress moved. Its own key is what a progress view
  // reads; a completed trash, restore, move or undo has also changed the tree,
  // so the trash and folder listings refresh with it.
  "file_operation.changed": (f) => [
    keys.files.operation(f.entity_id),
    keys.files.trashAll,
    keys.files.childrenAll,
  ],
};

/** Event types the portal deliberately maps to nothing today. Pinned by a test so an empty
 *  mapping is a decision, never an oversight. */
export const EVENT_TYPES_WITHOUT_A_SURFACE: ReadonlySet<RealtimeEventType> =
  new Set<RealtimeEventType>([]);

/** Invalidations landing within this window fold into one pass (the same coalescing window as
 *  the webview's daemon refresh). */
export const REFRESH_DEBOUNCE_MS = 250;

export function isRealtimeEventType(value: string): value is RealtimeEventType {
  return Object.prototype.hasOwnProperty.call(EVENT_KEYS, value);
}

/** The frame a `data:` body describes, or null when it is not one this client understands —
 *  malformed JSON, a shape the server does not send, or a type from a newer server. A frame is
 *  dropped, never thrown on: the stream must survive a forward-incompatible neighbour. */
export function parseFrame(type: string, data: string): RealtimeEventFrame | null {
  if (!isRealtimeEventType(type)) return null;
  let raw: unknown;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof raw !== "object" || raw === null) return null;
  const body = raw as Record<string, unknown>;
  if (body.type !== type) return null;
  if (typeof body.entity !== "string" || body.entity === "") return null;
  if (typeof body.entity_id !== "string" || body.entity_id === "") return null;
  if (typeof body.org_id !== "string" || body.org_id === "") return null;
  const version = body.version === undefined ? 0 : body.version;
  if (typeof version !== "number" || !Number.isInteger(version) || version < 0) return null;
  return {
    type,
    entity: body.entity,
    entity_id: body.entity_id,
    version,
    org_id: body.org_id,
    // The optional ids narrow a refetch, so anything that is not a non-empty
    // string is simply not there: a malformed one must widen the refresh, never
    // become a cache slot of its own.
    ...id(body.drive_id, "drive_id"),
    ...id(body.parent_id, "parent_id"),
    ...id(body.reason, "reason"),
    ...id(body.lease_node_id, "lease_node_id"),
    // Only the server's own `true` widens a refresh; anything else is absent.
    ...(body.subtree === true ? { subtree: true } : {}),
  };
}

/** One optional id, present only when the wire really carried one. */
function id(value: unknown, name: string): Record<string, string> {
  return typeof value === "string" && value !== "" ? { [name]: value } : {};
}

export interface InvalidationScheduler {
  /** Queue the keys a frame names; they flush together after the debounce window. */
  push(frame: RealtimeEventFrame): void;
  /** Queue an invalidate-everything (a `reset`, a resume): it supersedes every queued key. */
  reset(): void;
  /** Run whatever is queued now. */
  flush(): void;
  dispose(): void;
}

export interface SchedulerTimers {
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
}

const DEFAULT_SCHEDULER_TIMERS: SchedulerTimers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/**
 * Coalesce frames into cache invalidations. Keys are deduplicated by their react-query hash,
 * one timer covers the window, and each unique key is invalidated once as a PREFIX (the same
 * matching the shared mutation policy uses), so a burst of twenty run frames costs one refetch
 * of the run list. A `reset` clears the queue and invalidates everything instead.
 *
 * Gone-ness is per-resource, and a frame ABOUT a resource resets it. So each key is flushed in
 * two passes:
 *
 *  - the key EXACTLY, with no carve-out. A frame that names an entity is the server saying
 *    something about that entity right now, which is reason enough to ask again even if the
 *    last answer was a 404. This repo renders a grantable denial as an opaque 404, so "you may
 *    read this after all" arrives as exactly this shape — a `file_node.changed` naming the node
 *    a share was just granted on, or a `chat.updated` naming the chat. Without this pass the
 *    dead end never cleared: nothing else re-reads it, because window-focus refetching is off
 *    and the chat row's own poll stops on a 404.
 *  - the key as a PREFIX, skipping reads the server has already answered with "gone", and
 *    skipping the one the exact pass just took so it is not fetched twice. The storm this
 *    guards against is entirely here: a busy box emits node frames several times a second and
 *    every one of them names `["chats"]`, which prefixes the row of the chat somebody deleted
 *    while this tab had it open.
 *
 * A `reset` invalidates EVERYTHING with no carve-out. It means the stream was lost and this
 * client does not know what happened in the gap — the one pass where re-asking a 404 is most
 * warranted, because a grant is exactly the kind of thing that lands in a gap.
 *
 * A frame a machine raises at the rate it saves (`isMachineRate`) skips the queue: its keys go
 * through the throttle every surface on the client shares (`machineRefresh`), one leading read
 * and at most one trailing read per key per window, and a node's item or listing the cache
 * already holds at the frame's version is not read at all.
 */
export function createInvalidationScheduler(
  queryClient: QueryClient,
  opts: { debounceMs?: number; timers?: SchedulerTimers } = {},
): InvalidationScheduler {
  const debounceMs = opts.debounceMs ?? REFRESH_DEBOUNCE_MS;
  const timers = opts.timers ?? DEFAULT_SCHEDULER_TIMERS;
  const queued = new Map<string, QueryKey>();
  let resetPending = false;
  let timer: unknown = null;
  let disposed = false;

  const clear = (): void => {
    if (timer !== null) {
      timers.clearTimeout(timer);
      timer = null;
    }
  };
  const flush = (): void => {
    clear();
    if (disposed) return;
    if (resetPending) {
      resetPending = false;
      queued.clear();
      queryClient.invalidateQueries().catch(() => undefined);
      return;
    }
    const batch = [...queued.values()];
    queued.clear();
    for (const queryKey of batch) {
      const named = hashKey(queryKey);
      queryClient.invalidateQueries({ queryKey, exact: true }).catch(() => undefined);
      queryClient
        .invalidateQueries({
          queryKey,
          predicate: (query) => query.queryHash !== named && notGone(query),
        })
        .catch(() => undefined);
    }
  };
  const throttled = (frame: RealtimeEventFrame, keysFor: KeysFor): void => {
    if (frame.type === "file_node.changed" && frame.parent_id) {
      refreshForSave(queryClient, frame, { listing: { driveId: frame.drive_id, parentId: frame.parent_id } });
      return;
    }
    if (frame.type === "file_node.changed") refreshForSave(queryClient, frame);
    const throttle = machineRefresh(queryClient);
    for (const key of keysFor(frame)) {
      if (frame.type === "file_node.changed" && hashKey(key) === hashKey(keys.files.item(frame.entity_id))) continue;
      throttle.request(key);
    }
  };
  const arm = (): void => {
    if (timer !== null || disposed) return;
    timer = timers.setTimeout(() => {
      timer = null;
      flush();
    }, debounceMs);
  };

  return {
    push(frame) {
      if (disposed) return;
      const keysFor = EVENT_KEYS[frame.type];
      if (!keysFor) return;
      if (isMachineRate(frame)) {
        throttled(frame, keysFor);
        return;
      }
      for (const key of keysFor(frame)) queued.set(hashKey(key), key);
      arm();
    },
    reset() {
      if (disposed) return;
      resetPending = true;
      arm();
    },
    flush,
    dispose() {
      disposed = true;
      clear();
      queued.clear();
      resetPending = false;
    },
  };
}

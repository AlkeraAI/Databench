// @vitest-environment node
//
// The event map is the portal's reaction to each server event. Pinned here: it covers exactly
// the server's vocabulary (read from the committed OpenAPI document, so a stale generated type
// or a hand cast cannot hide drift), every key it names is one a hook really uses, an empty
// mapping is an explicit decision, a frame is parsed defensively, and the scheduler coalesces
// a burst into one invalidation per unique key — as a PREFIX, the way the mutation policy does.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { hashKey, type QueryKey } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";


import {
  EVENT_KEYS,
  EVENT_TYPES_WITHOUT_A_SURFACE,
  REFRESH_DEBOUNCE_MS,
  createInvalidationScheduler,
  isRealtimeEventType,
  parseFrame,
  type RealtimeEventFrame,
  type RealtimeEventType,
} from "@/api/events/eventMap";
import { allStaticKeys, keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { chatKeys } from "@/pages/workspace/chat/chatKeys";

const OPENAPI_PATH = fileURLToPath(
  new URL("../../../../../../packages/shared-openapi/openapi.json", import.meta.url),
);

const SCHEMA_DTS_PATH = fileURLToPath(
  new URL("../../../../../../packages/ts-sdk/src/schema.d.ts", import.meta.url),
);

function serverEventTypes(): string[] {
  const doc = JSON.parse(readFileSync(OPENAPI_PATH, "utf-8")) as {
    components: { schemas: Record<string, { enum?: string[] }> };
  };
  const schema = doc.components.schemas.RealtimeEventType;
  if (!schema?.enum) throw new Error("openapi.json carries no RealtimeEventType enum");
  return schema.enum;
}

/** The members of the generated `RealtimeEventType` union, read out of the committed
 *  `schema.d.ts`. It is the artifact `EVENT_KEYS` is typed exhaustive against, and it is
 *  generated from `openapi.json` by a separate step — so reading both and comparing them
 *  is what catches a regenerated schema that never reached the SDK (or the reverse). */
function generatedUnionTypes(): string[] {
  const source = readFileSync(SCHEMA_DTS_PATH, "utf-8");
  const declaration = /^\s*RealtimeEventType:\s*("[^;]+);/m.exec(source);
  if (!declaration) throw new Error("schema.d.ts carries no RealtimeEventType union");
  return [...declaration[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

const ORG = "3f9d2c1e-0000-4000-8000-000000000000";
const ID = "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f";
const DRIVE = "8a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d";
const PARENT = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f";

const frameOf = (type: RealtimeEventType, entity_id = ID): RealtimeEventFrame => ({
  type,
  entity: "thing",
  entity_id,
  version: 1,
  org_id: ORG,
});

/** `candidate` is `known` or a prefix of it, element by element (react-query's partial match). */
function isPrefixOf(candidate: QueryKey, known: QueryKey): boolean {
  if (candidate.length > known.length) return false;
  return candidate.every((part, i) => hashKey([part]) === hashKey([known[i]]));
}

describe("EVENT_KEYS", () => {
  it("covers exactly the RealtimeEventType enum in openapi.json", () => {
    expect(new Set(Object.keys(EVENT_KEYS))).toEqual(new Set(serverEventTypes()));
  });

  it("the server vocabulary is exactly the generated union, minus the server-only types", () => {
    const generated = generatedUnionTypes();
    expect(generated.length).toBeGreaterThan(0);
    expect(serverEventTypes()).toHaveLength(generated.length);
    expect(new Set(serverEventTypes())).toEqual(new Set(generated));
    expect(serverEventTypes()).not.toContain("doc.op");
    expect(serverEventTypes()).not.toContain("authz.decision");
  });

  it("the Files events are part of the client vocabulary", () => {
    expect(serverEventTypes()).toContain("file_node.changed");
    expect(serverEventTypes()).toContain("file_operation.changed");
  });

  it("every mapped key is a portal key or a prefix of one", () => {
    const known: QueryKey[] = [
      ...allStaticKeys(),
      keys.gate.run(ID),
      keys.teamConnections.verification(ID),
      keys.chats.one(ID),
      keys.chats.messages(ID),
      keys.objects.one(ID),
      keys.files.item(ID),
      keys.files.operation(ID),
      keys.files.childrenOf(DRIVE, PARENT),
      keys.chatTemplates.one(ID),
      keys.machines.workspace(ID),
      // The chat pages keep their own single spelling of the chat-list read
      // (the rail, the header and the crumb trail share one entry); a frame may
      // name it, and nothing else.
      chatKeys.chats(),
    ];
    for (const type of Object.keys(EVENT_KEYS) as RealtimeEventType[]) {
      for (const key of EVENT_KEYS[type](frameOf(type))) {
        const matches = known.some((k) => isPrefixOf(key, k));
        expect(matches, `${type} → ${JSON.stringify(key)} names no portal key`).toBe(true);
      }
    }
  });

  it("every type maps to at least one key, except the ones pinned as surfaceless", () => {
    for (const type of Object.keys(EVENT_KEYS) as RealtimeEventType[]) {
      const count = EVENT_KEYS[type](frameOf(type)).length;
      if (EVENT_TYPES_WITHOUT_A_SURFACE.has(type)) expect(count, type).toBe(0);
      else expect(count, type).toBeGreaterThan(0);
    }
  });

  it("every type the server can deliver now has a surface that reads it", () => {
    expect([...EVENT_TYPES_WITHOUT_A_SURFACE]).toEqual([]);
  });

  it.each([
    ["gate_run.ingested", [keys.gate.runsAll, keys.gate.summary, keys.gate.run(ID), keys.gate.activityAll, keys.gate.status, keys.gate.driftAll]],
    [
      "connection.verification_changed",
      [keys.teamConnections.verification(ID), keys.teamConnections.all, keys.connections.me],
    ],
    ["connection.credential_changed", [keys.teamConnections.all, keys.connections.me]],
    // The Connections page shows the same rows a team's admin list does, to
    // whoever can use them, so every connection frame has to reach it too — the
    // page is what a running daemon's badge change is read on.
    [
      "team_connection.probed",
      [
        keys.teamConnections.all,
        keys.connections.me,
        keys.connectionInventory.me,
        keys.connectionInventory.org,
      ],
    ],
    [
      "team_connection.updated",
      [
        keys.teamConnections.all,
        keys.connections.me,
        keys.connectionInventory.me,
        keys.connectionInventory.org,
      ],
    ],
    ["kb_item.changed", [keys.kb.all]],
    ["artifact.updated", [keys.kb.all]],
    ["user.email_verified", [keys.auth.me]],
    ["invitation.changed", [keys.invitations.all]],
    // A join, a leave or a role change rewrites more than the rosters: it decides
    // which connections the person may use, what `/auth/me` says they administer,
    // and every per-member row the org's billing and storage tables draw.
    [
      "membership.changed",
      [
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
    ],
    // The rail/header/crumb read the chat list under the chat pages' own key, so the
    // frame names it beside the portal family: a rename lands live on both.
    ["chat.updated", [keys.chats.all, keys.chats.one(ID), chatKeys.chats(), keys.workspaces.all]],
    // A saved chat template is a workspace object: the templates browser and the
    // template's own page read it, and neither hangs under the objects prefix.
    // ...and a chat is one as well. The object route is where a chat is renamed,
    // and it announces this frame and nothing else, so the chat's own entries
    // ride along or a rename made elsewhere is invisible until a reload.
    [
      "workspace_object.changed",
      [
        keys.workspaces.all,
        keys.objects.all,
        keys.objects.one(ID),
        keys.chatTemplates.all,
        keys.chatTemplates.one(ID),
        keys.chats.all,
        keys.chats.one(ID),
        chatKeys.chats(),
      ],
    ],
    // A chat row carries the machine's status, and the rail reads the chat list
    // under the chat pages' own key — so a machine frame has to name it for the
    // same reason a rename does.
    [
      "compute_machine.changed",
      [keys.machines.current, keys.chats.all, chatKeys.chats()],
    ],
    // An org machine's state is read by the banner and the chat rows, like the workspace
    // machine's.
    // The Machines pages and every workspace chip read it as well.
    [
      "org_machine.changed",
      [keys.machines.current, keys.machines.org, keys.machines.workspaceAll, keys.chats.all, chatKeys.chats()],
    ],
    // A move changes where the workspace and every chat in it runs; the workspace's own
    // machine read carries the move's step, which the header chip shows.
    [
      "workspace.machine_move",
      [
        keys.workspaces.all,
        keys.objects.one(ID),
        keys.machines.workspace(ID),
        keys.machines.org,
        keys.machines.current,
        keys.chats.all,
        chatKeys.chats(),
      ],
    ],
    // A node change refreshes the node itself and every listing prefix it could sit under —
    // a move leaves one folder for another, so the old listing is stale too.
    [
      "file_node.changed",
      [
        keys.files.item(ID),
        keys.files.childrenAll,
        keys.files.permissionsAll,
        keys.files.searchAll,
        keys.files.recent,
        keys.files.starred,
        keys.files.sharedWithMe,
        keys.files.trashAll,
        keys.files.leasesAll,
        keys.chats.all,
        chatKeys.chats(),
      ],
    ],
    [
      "file_operation.changed",
      [keys.files.operation(ID), keys.files.trashAll, keys.files.childrenAll],
    ],
  ] as [RealtimeEventType, QueryKey[]][])("%s names its slots", (type, expected) => {
    expect(EVENT_KEYS[type](frameOf(type))).toEqual(expected);
  });

  it.each([
    "chat.updated",
    "compute_machine.changed",
    "workspace_object.changed",
    // A chat's node carries its sharing, and the list is cut by what the reader
    // may reach — so a grant or a revoke adds or removes a whole row.
    "file_node.changed",
  ] as RealtimeEventType[])(
    "%s refreshes the entry the chat rail actually renders",
    (type) => {
      // The rail, the chat's header and its crumb trail all draw a chat's title
      // and its machine's state from ONE entry of its own. Every frame that can
      // move either fact has to name it: the machine badge on every row moves
      // for the machine frame, and a rename arrives as a chat frame or — since
      // a chat is renamed through the object route — as an object one. Without
      // the same entry they keep showing what the page was opened with.
      expect(EVENT_KEYS[type](frameOf(type))).toContainEqual([...chatKeys.chats()]);
    },
  );

  it.each([
    ["the connections a person may use", keys.connections.me],
    ["the team's own connection list", keys.teamConnections.all],
    ["what /auth/me says they administer", keys.auth.me],
    ["the org billing members table", keys.org.billing],
    ["the per-member storage roster", keys.org.storage],
    ["a member's plan, tier and usage", keys.teams.memberPlanAll],
  ])("a membership frame refreshes %s", (_surface, key) => {
    // Each of these is read by a surface that shows a person's membership as a
    // FACT — a connection they inherit from a team, an admin badge, a row in a
    // roster — and none of them hangs under the members prefix. Without the key
    // the screen keeps the roster it was opened with until a reload.
    expect(EVENT_KEYS["membership.changed"](frameOf("membership.changed"))).toContainEqual([
      ...key,
    ]);
  });

  it("an object frame refreshes the workspaces; a node frame leaves them to the chat list", () => {
    // A teammate's new workspace or rename arrives as an object frame.
    expect(EVENT_KEYS["workspace_object.changed"](frameOf("workspace_object.changed"))).toContainEqual([
      ...keys.workspaces.all,
    ]);
    // A node frame is what a box sends for every save, so the workspace list
    // does not re-read per frame: a share shows first in the chat list, which
    // the rail reconciles the workspaces against.
    for (const frame of [frameOf("file_node.changed", "n-9"), { ...frameOf("file_node.changed", "n-9"), reason: "live_saved" }]) {
      expect(EVENT_KEYS["file_node.changed"](frame)).not.toContainEqual([...keys.workspaces.all]);
    }
  });

  it("a node frame refreshes the chat list, which is cut by what the reader may reach", () => {
    // Granting or revoking a chat's node changes WHICH chats the other person
    // may list, and their session hears only this frame — the grant was written
    // by somebody else's mutation, so no `meta.invalidates` can reach them. A
    // grant carries no reason; a reason this client does not know comes from a
    // newer server and has to keep the refresh, never silently drop it.
    for (const frame of [
      frameOf("file_node.changed", "n-9"),
      { ...frameOf("file_node.changed", "n-9"), drive_id: DRIVE, parent_id: PARENT },
      { ...frameOf("file_node.changed", "n-9"), reason: "a_reason_from_a_newer_server" },
    ]) {
      expect(EVENT_KEYS["file_node.changed"](frame)).toContainEqual([...keys.chats.all]);
      expect(EVENT_KEYS["file_node.changed"](frame)).toContainEqual([...chatKeys.chats()]);
    }
  });

  it.each([["live_saved"], ["inbound_superseded"], ["live_batch"], ["conflict"]])(
    "a %s node frame leaves the chat list alone",
    (reason) => {
      // These reasons are a machine's, not a person's, and they arrive many
      // times a second while an agent writes — to every signed-in member of the
      // org, since the frame is org-visible. Neither can change which chats a
      // reader may list, while `chats` is a PREFIX of every chat entry and the
      // list behind it is a sequential multi-page read: left in, an ordinary
      // save re-runs that enumeration on every portal seat four times a second.
      const machine = EVENT_KEYS["file_node.changed"]({
        ...frameOf("file_node.changed", "n-9"),
        drive_id: DRIVE,
        parent_id: PARENT,
        reason,
      });
      expect(machine.some((k) => hashKey(k) === hashKey(keys.chats.all))).toBe(false);
      expect(machine.some((k) => hashKey(k) === hashKey(chatKeys.chats()))).toBe(false);
      // The save IS a change to the file, so the node's own reads still refresh —
      // and no wider than the folder the frame names.
      expect(machine).toContainEqual([...keys.files.item("n-9")]);
      expect(machine).toContainEqual([...keys.files.childrenOf(DRIVE, PARENT)]);
      expect(machine).not.toContainEqual([...keys.files.childrenAll]);
    },
  );

  it("a conflict frame refreshes the node and the folder its copy lands in, and nothing else", () => {
    // The drive kept both versions: the original under its name and the other
    // as a conflicted copy beside it. The row and the folder listing the copy
    // lands in are the answers that moved; none of the wide fan-out did.
    const conflict = EVENT_KEYS["file_node.changed"]({
      ...frameOf("file_node.changed", "n-9"),
      drive_id: DRIVE,
      parent_id: PARENT,
      reason: "conflict",
    });
    expect(conflict.map(hashKey).sort()).toEqual(
      [keys.files.item("n-9"), keys.files.childrenOf(DRIVE, PARENT)].map(hashKey).sort(),
    );
  });

  it("a tree batch's folder frame refreshes that folder and its item, and nothing else", () => {
    // The holder reports its disk several times a second while an agent works,
    // one frame per folder the batch touched. Each is a folder whose listing
    // moved; none is a grant, a star, a trash or a search result, and the wide
    // fan-out on every one of them is what tripped the API's rate limiter.
    const batch = EVENT_KEYS["file_node.changed"]({
      ...frameOf("file_node.changed", PARENT),
      drive_id: DRIVE,
      parent_id: PARENT,
      reason: "live_batch",
    });
    expect(batch.map(hashKey).sort()).toEqual(
      [keys.files.item(PARENT), keys.files.childrenOf(DRIVE, PARENT)].map(hashKey).sort(),
    );
  });

  it.each([
    { name: "the server's true", extra: { subtree: true }, carried: true },
    { name: "false", extra: { subtree: false }, carried: false },
    { name: "a string", extra: { subtree: "true" }, carried: false },
    { name: "a number", extra: { subtree: 1 }, carried: false },
    { name: "absent", extra: {}, carried: false },
  ])(
    "parseFrame keeps a lease frame's subtree flag when it is $name: $carried",
    ({ extra, carried }) => {
      const frame = parseFrame(
        "file_lease.changed",
        JSON.stringify({
          type: "file_lease.changed",
          entity: "file_lease",
          entity_id: "n-9",
          version: 3,
          org_id: ORG,
          lease_node_id: "n-9",
          ...extra,
        }),
      );
      expect(frame?.subtree).toBe(carried ? true : undefined);
    },
  );

  it("a subtree lease frame refreshes no wider than any other lease frame", () => {
    // The open folders under the lease are the hook's to refresh; the
    // drive-wide listing prefix here would re-read every folder ever opened.
    const lease = { ...frameOf("file_lease.changed", "n-9"), lease_node_id: "n-9" };
    expect(EVENT_KEYS["file_lease.changed"]({ ...lease, subtree: true }).map(hashKey)).toEqual(
      EVENT_KEYS["file_lease.changed"](lease).map(hashKey),
    );
  });

  it("a node frame that names its folder refreshes that folder and not the whole drive", () => {
    // A file written into an open chat folder arrives many times a second. With
    // only the family prefix, each one re-fetches every folder listing the
    // browser holds; with the parent named, one folder refreshes.
    const named = EVENT_KEYS["file_node.changed"]({
      ...frameOf("file_node.changed", "n-9"),
      drive_id: DRIVE,
      parent_id: PARENT,
    });
    expect(named).toContainEqual([...keys.files.childrenOf(DRIVE, PARENT)]);
    expect(named).not.toContainEqual([...keys.files.childrenAll]);
  });

  it("a node frame with no folder still refreshes every listing", () => {
    // The parent is optional on the wire and absent from an older server: the
    // fallback has to be the wasted refetch, never a stale folder.
    const bare = EVENT_KEYS["file_node.changed"](frameOf("file_node.changed", "n-9"));
    expect(bare).toContainEqual([...keys.files.childrenAll]);
    expect(bare.some((k) => hashKey(k) === hashKey(keys.files.childrenOf(DRIVE, PARENT)))).toBe(
      false,
    );
    // A parent with no drive cannot name a folder entry, so it falls back too.
    const halfNamed = EVENT_KEYS["file_node.changed"]({
      ...frameOf("file_node.changed", "n-9"),
      parent_id: PARENT,
    });
    expect(halfNamed).toContainEqual([...keys.files.childrenAll]);
  });

  it("a node frame refreshes who can reach it", () => {
    // Sharing a node is a node change and nothing else: the share dialog's two
    // reads are the only place the new grant shows.
    for (const frame of [
      frameOf("file_node.changed", "n-9"),
      { ...frameOf("file_node.changed", "n-9"), drive_id: DRIVE, parent_id: PARENT },
    ]) {
      expect(EVENT_KEYS["file_node.changed"](frame)).toContainEqual([...keys.files.permissionsAll]);
    }
  });

  it("the frame a box's attribute restore sends refreshes the file and its folder only", () => {
    // After each save a box restores the file's mode and mtime. The server
    // announces that write the way it announces the save itself: the holder's
    // reason and the folder the file is in. Read off the wire as the socket
    // delivers it, it must refetch the file and that folder's listing and not
    // the chat list, the other listings or sharing.
    const frame = parseFrame(
      "file_node.changed",
      JSON.stringify({
        type: "file_node.changed",
        entity: "file_node",
        entity_id: "n-9",
        version: 4,
        org_id: ORG,
        drive_id: DRIVE,
        parent_id: PARENT,
        reason: "live_saved",
      }),
    );
    expect(frame).not.toBeNull();
    const refetched = EVENT_KEYS["file_node.changed"](frame as RealtimeEventFrame).map(hashKey);
    expect(refetched.sort()).toEqual(
      [keys.files.item("n-9"), keys.files.childrenOf(DRIVE, PARENT)].map(hashKey).sort(),
    );
    expect(refetched).not.toContain(hashKey(keys.chats.all));
    expect(refetched).not.toContain(hashKey(chatKeys.chats()));
  });

  it("parseFrame carries the folder and the reason through, and drops a malformed one", () => {
    const body = JSON.stringify({
      type: "file_node.changed",
      entity: "file_node",
      entity_id: "n-9",
      version: 3,
      org_id: ORG,
      drive_id: DRIVE,
      parent_id: PARENT,
      reason: "live_saved",
    });
    expect(parseFrame("file_node.changed", body)).toEqual({
      type: "file_node.changed",
      entity: "file_node",
      entity_id: "n-9",
      version: 3,
      org_id: ORG,
      drive_id: DRIVE,
      parent_id: PARENT,
      reason: "live_saved",
    });
    // A null parent is what the server sends for a root, and is not a folder.
    const root = parseFrame(
      "file_node.changed",
      JSON.stringify({
        type: "file_node.changed",
        entity: "file_node",
        entity_id: "n-9",
        version: 3,
        org_id: ORG,
        parent_id: null,
        reason: null,
      }),
    );
    expect(root?.parent_id).toBeUndefined();
    expect(root?.reason).toBeUndefined();
    // A parent that is not an id is dropped rather than used as a cache slot.
    const bogus = parseFrame(
      "file_node.changed",
      JSON.stringify({
        type: "file_node.changed",
        entity: "file_node",
        entity_id: "n-9",
        version: 3,
        org_id: ORG,
        parent_id: 17,
      }),
    );
    expect(bogus?.parent_id).toBeUndefined();
  });

  it("an entity-scoped type threads the frame's entity id into its key", () => {
    expect(
      EVENT_KEYS["connection.verification_changed"](
        frameOf("connection.verification_changed", "v-9"),
      ),
    ).toContainEqual(["connection-verification", "v-9"]);
    expect(EVENT_KEYS["gate_run.ingested"](frameOf("gate_run.ingested", "r-9"))).toContainEqual([
      "gate",
      "run",
      "r-9",
    ]);
    expect(EVENT_KEYS["file_node.changed"](frameOf("file_node.changed", "n-9"))).toContainEqual([
      "files",
      "item",
      "n-9",
    ]);
    expect(
      EVENT_KEYS["file_operation.changed"](frameOf("file_operation.changed", "op-9")),
    ).toContainEqual(["files", "operation", "op-9"]);
  });
});

describe("isRealtimeEventType / parseFrame", () => {
  it("recognises every server type and nothing else", () => {
    for (const type of serverEventTypes()) expect(isRealtimeEventType(type)).toBe(true);
    expect(isRealtimeEventType("doc.op")).toBe(false);
    expect(isRealtimeEventType("toString")).toBe(false);
    expect(isRealtimeEventType("")).toBe(false);
  });

  const good = { type: "kb_item.changed", entity: "kb_item", entity_id: ID, version: 3, org_id: ORG };

  it("parses a well-formed frame", () => {
    expect(parseFrame("kb_item.changed", JSON.stringify(good))).toEqual(good);
  });

  it("defaults a missing version to 0", () => {
    const rest = { type: good.type, entity: good.entity, entity_id: good.entity_id, org_id: good.org_id };
    expect(parseFrame("kb_item.changed", JSON.stringify(rest))).toEqual({ ...rest, version: 0 });
  });

  it.each([
    ["malformed json", "kb_item.changed", "{not json"],
    ["a newer server's type", "kb_item.renamed", JSON.stringify({ ...good, type: "kb_item.renamed" })],
    ["a type that disagrees with the body", "artifact.updated", JSON.stringify(good)],
    ["a non-object body", "kb_item.changed", JSON.stringify("kb_item.changed")],
    ["a null body", "kb_item.changed", "null"],
    ["a missing entity", "kb_item.changed", JSON.stringify({ ...good, entity: undefined })],
    ["an empty entity_id", "kb_item.changed", JSON.stringify({ ...good, entity_id: "" })],
    ["a missing org_id", "kb_item.changed", JSON.stringify({ ...good, org_id: undefined })],
    ["a negative version", "kb_item.changed", JSON.stringify({ ...good, version: -1 })],
    ["a fractional version", "kb_item.changed", JSON.stringify({ ...good, version: 1.5 })],
    ["a string version", "kb_item.changed", JSON.stringify({ ...good, version: "3" })],
  ])("drops %s rather than throwing", (_name, type, data) => {
    expect(parseFrame(type, data)).toBeNull();
  });
});

describe("createInvalidationScheduler", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  const spied = () => {
    const qc = createQueryClient({ retry: false });
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    return { qc, invalidate };
  };

  it("coalesces frames within the window into one invalidation per unique key", async () => {
    const { qc, invalidate } = spied();
    const scheduler = createInvalidationScheduler(qc);
    scheduler.push(frameOf("kb_item.changed"));
    scheduler.push(frameOf("kb_item.changed"));
    scheduler.push(frameOf("artifact.updated")); // the same ["kb"] key again
    scheduler.push(frameOf("gate_run.ingested", "r1"));
    scheduler.push(frameOf("gate_run.ingested", "r1"));
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS - 1);
    expect(invalidate).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    // ["kb"] once + the six gate keys once each — each as two passes, the
    // named key exactly and then the prefix under it.
    expect(invalidate).toHaveBeenCalledTimes(14);
    const called = invalidate.mock.calls.map((c) => hashKey((c[0] as { queryKey: QueryKey }).queryKey));
    expect(new Set(called).size).toBe(7);
    expect(called).toContain(hashKey(keys.kb.all));
    expect(called).toContain(hashKey(keys.gate.run("r1")));
    scheduler.dispose();
  });

  it("two frames for different entities of one type keep their own entity keys", async () => {
    const { qc, invalidate } = spied();
    const scheduler = createInvalidationScheduler(qc);
    scheduler.push(frameOf("connection.verification_changed", "v1"));
    scheduler.push(frameOf("connection.verification_changed", "v2"));
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS);
    // The two record keys, plus the two list prefixes both frames share, each
    // flushed as a named pass and a prefix pass.
    expect(invalidate).toHaveBeenCalledTimes(8);
    const keySet = new Set(
      invalidate.mock.calls.map((c) => hashKey((c[0] as { queryKey: QueryKey }).queryKey)),
    );
    expect(keySet).toEqual(
      new Set(
        [
          ["connection-verification", "v1"],
          ["connection-verification", "v2"],
          keys.teamConnections.all,
          keys.connections.me,
        ].map((k) => hashKey(k as QueryKey)),
      ),
    );
    // A named key is asked for exactly, with nothing carved out of it.
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["connection-verification", "v1"],
      exact: true,
    });
    scheduler.dispose();
  });

  it("a frame after the window opens a new window", async () => {
    const { qc, invalidate } = spied();
    const scheduler = createInvalidationScheduler(qc);
    scheduler.push(frameOf("kb_item.changed"));
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS);
    scheduler.push(frameOf("kb_item.changed"));
    expect(invalidate).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS);
    expect(invalidate).toHaveBeenCalledTimes(4);
    scheduler.dispose();
  });

  it("reset invalidates everything once and drops the queued keys", async () => {
    const { qc, invalidate } = spied();
    const scheduler = createInvalidationScheduler(qc);
    scheduler.push(frameOf("kb_item.changed"));
    scheduler.reset();
    scheduler.push(frameOf("gate_run.ingested"));
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS);
    expect(invalidate).toHaveBeenCalledTimes(1);
    // With no carve-out: a reset means the gap is unknown, so a read the server
    // last refused is exactly the one worth asking again.
    expect(invalidate).toHaveBeenCalledWith();
    scheduler.dispose();
  });

  it("dispose cancels a pending flush and ignores later pushes", async () => {
    const { qc, invalidate } = spied();
    const scheduler = createInvalidationScheduler(qc);
    scheduler.push(frameOf("kb_item.changed"));
    scheduler.dispose();
    scheduler.push(frameOf("kb_item.changed"));
    scheduler.reset();
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS * 2);
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("flush runs the queue immediately and cancels the timer", async () => {
    const { qc, invalidate } = spied();
    const scheduler = createInvalidationScheduler(qc);
    scheduler.push(frameOf("kb_item.changed"));
    scheduler.flush();
    expect(invalidate).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS);
    expect(invalidate).toHaveBeenCalledTimes(2);
    scheduler.dispose();
  });

  it("a key is invalidated as a prefix: every query under it goes stale, its neighbours do not", async () => {
    const qc = createQueryClient({ retry: false });
    qc.setQueryData(keys.gate.runs(), ["a"]);
    qc.setQueryData(keys.gate.runs("acme/warehouse"), ["b"]);
    qc.setQueryData(keys.gate.run("r1"), { id: "r1" });
    qc.setQueryData(keys.billing.summary, { tier: "plus" });
    const scheduler = createInvalidationScheduler(qc);
    scheduler.push(frameOf("gate_run.ingested", "r1"));
    await vi.advanceTimersByTimeAsync(REFRESH_DEBOUNCE_MS);
    expect(qc.getQueryState(keys.gate.runs())?.isInvalidated).toBe(true);
    expect(qc.getQueryState(keys.gate.runs("acme/warehouse"))?.isInvalidated).toBe(true);
    expect(qc.getQueryState(keys.gate.run("r1"))?.isInvalidated).toBe(true);
    expect(qc.getQueryState(keys.billing.summary)?.isInvalidated).toBe(false);
    scheduler.dispose();
  });

  it("the debounce window is the webview's 250 ms", () => {
    expect(REFRESH_DEBOUNCE_MS).toBe(250);
  });
});

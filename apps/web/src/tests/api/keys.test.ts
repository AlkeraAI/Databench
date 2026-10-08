import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createElement, type ReactElement, type ReactNode } from "react";
import { hashKey, QueryClientProvider, type QueryKey } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { meKey } from "@/api/auth";
import { allStaticKeys, keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import {
  type TeamConnectionUpsert,
  useDeleteTeamConnection,
  useRotateTeamConnectionSecret,
  useUpsertTeamConnection,
} from "@/api/teamConnections";

// api/keys.ts is the one place a portal query key is spelled. These cases pin the
// shape every consumer relies on (flat, primitive, collision-free), that no api
// module has grown a second spelling, and that the aliases older callers import
// still point at the hoisted tuples rather than at a copy.

type Primitive = string | number | boolean | null | undefined;

const isPrimitive = (v: unknown): v is Primitive =>
  v === null || v === undefined || ["string", "number", "boolean"].includes(typeof v);

/** A plain record of primitives — the one non-primitive element a key may carry
 *  (the audit page's filter object), which react-query hashes with sorted keys. */
const isPrimitiveRecord = (v: unknown): boolean =>
  typeof v === "object" &&
  v !== null &&
  !Array.isArray(v) &&
  Object.getPrototypeOf(v) === Object.prototype &&
  Object.values(v as Record<string, unknown>).every(isPrimitive);

/** Dotted paths of every function-valued entry under `keys`, so the sample table
 *  below is checked for completeness rather than trusted. */
function functionPaths(node: unknown, prefix = ""): string[] {
  if (typeof node === "function") return [prefix];
  if (Array.isArray(node) || node === null || typeof node !== "object") return [];
  return Object.entries(node as Record<string, unknown>).flatMap(([k, v]) =>
    functionPaths(v, prefix ? `${prefix}.${k}` : k),
  );
}

// One sample invocation per parameterised key. Adding a parameterised key
// without a row here fails the completeness case.
const SAMPLED: Record<string, QueryKey> = {
  "auth.deviceInfo": keys.auth.deviceInfo("ABCD-1234"),
  "oauth.registerContext": keys.oauth.registerContext("ticket"),
  "invitations.byToken": keys.invitations.byToken("tok"),
  "invitations.team": keys.invitations.team("t1"),
  "me.usage": keys.me.usage("30d"),
  "chats.one": keys.chats.one("c1"),
  "chats.attachments": keys.chats.attachments("c1"),
  "chats.messages": keys.chats.messages("c1"),
  "chatWorkspace.one": keys.chatWorkspace.one("c1"),
  "workspaces.one": keys.workspaces.one("w1"),
  "machines.orgOne": keys.machines.orgOne("m1"),
  "machines.currentFor": keys.machines.currentFor("w1"),
  "machines.workspace": keys.machines.workspace("w1"),
  "machines.usage": keys.machines.usage("2026-10-01", "2026-10-31", "t1"),
  "machines.quote": keys.machines.quote("o1", 100),
  "machines.diskQuote": keys.machines.diskQuote("m1", 200),
  "notebooks.view": keys.notebooks.view("d1", "n1"),
  "notebooks.stored": keys.notebooks.stored("d1", "n1", "v1"),
  "notebooks.storedOutput": keys.notebooks.storedOutput("c1", "p1", "s1"),
  "notebooks.envs": keys.notebooks.envs("d1", "n1"),
  "notebooks.connections": keys.notebooks.connections("d1", "n1"),
  "notebooks.editor": keys.notebooks.editor("d1", "n1"),
  "notebooks.packages": keys.notebooks.packages("d1", "n1", "e1"),
  "notebooks.packagesOf": keys.notebooks.packagesOf("d1", "n1"),
  "objects.ofType": keys.objects.ofType("result"),
  "objects.one": keys.objects.one("o1"),
  "objects.rows": keys.objects.rows("o1", 0, 50),
  "objects.chartRows": keys.objects.chartRows("o1"),
  "files.item": keys.files.item("n1"),
  "files.children": keys.files.children("d1", "n1", { orderBy: "name asc" }, 500),
  "files.childrenOf": keys.files.childrenOf("d1", "n1"),
  "files.places": keys.files.places("d1"),
  "files.search": keys.files.search("d1", { q: "report" }),
  "files.trash": keys.files.trash("d1"),
  "files.operation": keys.files.operation("op1"),
  "files.leases": keys.files.leases("d1"),
  "files.versions": keys.files.versions("n1"),
  "files.permissions": keys.files.permissions("n1"),
  "files.shareCandidates": keys.files.shareCandidates("n1", "dana"),
  "files.activity": keys.files.activity("n1"),
  "chatTemplates.one": keys.chatTemplates.one("tpl1"),
  "admin.orgStorage": keys.admin.orgStorage("o1"),
  "admin.orgLiveEditing": keys.admin.orgLiveEditing("o1"),
  "admin.orgChats": keys.admin.orgChats("o1"),
  "admin.orgErrors": keys.admin.orgErrors("o1"),
  "admin.orgAudit": keys.admin.orgAudit("o1"),
  "kb.attached": keys.kb.attached("snowflake://acct1/DB.PUBLIC.ORDERS"),
  "gate.runs": keys.gate.runs("acme/warehouse"),
  "gate.run": keys.gate.run("r1"),
  "gate.drift": keys.gate.drift(undefined),
  "gate.activity": keys.gate.activity(),
  "org.usage": keys.org.usage("7d"),
  "orgAdmin.audit": keys.orgAdmin.audit(0, 50, { action: "agent." }),
  "teams.members": keys.teams.members("t1", true),
  "teams.memberPlan": keys.teams.memberPlan("t1", "u1"),
  "teams.allocations": keys.teams.allocations("t1"),
  "teamConnections.team": keys.teamConnections.team("t1"),
  "teamConnections.verification": keys.teamConnections.verification(null),
  "admin.org": keys.admin.org("o1"),
  "admin.orgTeams": keys.admin.orgTeams("o1"),
  "admin.userAccount": keys.admin.userAccount("u1"),
  "admin.userExports": keys.admin.userExports("u1"),
  "admin.orgMembers": keys.admin.orgMembers("o1"),
  "admin.orgSettings": keys.admin.orgSettings("o1"),
  "admin.userIpInfo": keys.admin.userIpInfo("u1"),
  "admin.overview": keys.admin.overview("30d"),
  "admin.billingUser": keys.admin.billingUser("u1"),
  "admin.usageResetPreview": keys.admin.usageResetPreview("u1"),
  "admin.usageUser": keys.admin.usageUser("u1", "30d"),
  "admin.billingOrg": keys.admin.billingOrg("o1"),
  "admin.usageOrg": keys.admin.usageOrg("o1", "30d"),
  "admin.recurringGrantsOrg": keys.admin.recurringGrantsOrg("o1"),
  "admin.enterpriseOrg": keys.admin.enterpriseOrg("o1"),
  "admin.enterpriseOrgUsage": keys.admin.enterpriseOrgUsage("o1", 6),
  "admin.entitlements": keys.admin.entitlements("o1"),
  "admin.crashReports": keys.admin.crashReports(false),
  "admin.crashReport": keys.admin.crashReport("c1"),
  "admin.auditLogs": keys.admin.auditLogs(1, 25),
  "admin.computeOrg": keys.admin.computeOrg("o1"),
  "admin.slackOrg": keys.admin.slackOrg("o1"),
  "admin.orgDedicatedCompute": keys.admin.orgDedicatedCompute("o1"),
  "admin.adminMachinesFleet": keys.admin.adminMachinesFleet(true),
  "admin.adminMachinesDetail": keys.admin.adminMachinesDetail("m1"),
  "admin.orgMachines": keys.admin.orgMachines("o1"),
};

const everyKey = (): QueryKey[] => [...allStaticKeys(), ...Object.values(SAMPLED)];

describe("api/keys", () => {
  it("samples every parameterised key exactly once", () => {
    expect(new Set(Object.keys(SAMPLED))).toEqual(new Set(functionPaths(keys)));
  });

  it("collects every static key and nothing else", () => {
    const all = allStaticKeys();
    expect(all.length).toBeGreaterThan(40);
    expect(all).toContainEqual(["auth", "me"]);
    expect(all).toContainEqual(["public-config"]);
    expect(all).toContainEqual(["admin", "platform-cap"]);
    // A parameterised key is a function, never a tuple, so its spelling is not in the census.
    expect(all.some((k) => k.includes("device-info"))).toBe(false);
  });

  it("every key is a flat array of JSON primitives (a filter object only as a plain record of primitives)", () => {
    for (const key of everyKey()) {
      expect(Array.isArray(key)).toBe(true);
      expect(key.length).toBeGreaterThan(0);
      expect(typeof key[0]).toBe("string");
      for (const part of key) {
        expect(isPrimitive(part) || isPrimitiveRecord(part)).toBe(true);
      }
    }
  });

  it("no two keys collide", () => {
    const hashes = everyKey().map((k) => hashKey(k));
    expect(new Set(hashes).size).toBe(hashes.length);
  });

  it("a prefix key never equals the parameterised key it prefixes with an empty argument", () => {
    // `gate.runs()` spells the unfiltered list as ["gate","runs",""]; the family
    // prefix ["gate","runs"] must stay a strict prefix so a prefix invalidation
    // reaches every repo filter, the empty one included.
    expect(hashKey(keys.gate.runsAll)).not.toBe(hashKey(keys.gate.runs()));
    expect(hashKey(keys.gate.driftAll)).not.toBe(hashKey(keys.gate.drift()));
    expect(hashKey(keys.gate.activityAll)).not.toBe(hashKey(keys.gate.activity()));
    expect(keys.gate.runs().slice(0, keys.gate.runsAll.length)).toEqual([...keys.gate.runsAll]);
  });

  it("a folder's listing key separates the drive and the page size it asked for", () => {
    // Two readers of one folder — the browser and the soft-threshold footer —
    // ask for different page sizes. Sharing one entry would let whichever
    // mounted first decide the page the other renders.
    const parent = "nd_parent";
    const params = { orderBy: "name asc" };
    expect(hashKey(keys.files.children("d1", parent, params, 500))).not.toBe(
      hashKey(keys.files.children("d1", parent, params, 50)),
    );
    expect(hashKey(keys.files.children("d1", parent, params, 500))).not.toBe(
      hashKey(keys.files.children("d2", parent, params, 500)),
    );
    // The family prefix still reaches every one of them.
    expect(keys.files.children("d1", parent, params, 500).slice(0, 2)).toEqual([
      ...keys.files.childrenAll,
    ]);
  });

  it("a filter object hashes by content, not by property order", () => {
    const a = keys.orgAdmin.audit(0, 50, { action: "agent.", actor_email: "x@y" });
    const b = keys.orgAdmin.audit(0, 50, { actor_email: "x@y", action: "agent." });
    expect(hashKey(a)).toBe(hashKey(b));
  });

  it("the folder key an event names is a strict prefix of the key a listing caches under", () => {
    // A frame naming a parent can only refresh the folder it names if its key is
    // a prefix of every listing entry for that folder — the filters and the page
    // size ride the tail, and there is one entry per combination of them.
    const params = { orderBy: "name asc" };
    const narrow = keys.files.childrenOf("d1", "n1");
    for (const limit of [50, 500]) {
      const listing = keys.files.children("d1", "n1", params, limit);
      expect(listing.slice(0, narrow.length)).toEqual([...narrow]);
    }
    // ...and strictly narrower than the family prefix, so a frame that names a
    // parent does NOT invalidate every folder in the drive.
    expect(hashKey(narrow)).not.toBe(hashKey(keys.files.childrenAll));
    expect(narrow.slice(0, keys.files.childrenAll.length)).toEqual([...keys.files.childrenAll]);
    // A different folder, and a different drive, are different entries.
    expect(hashKey(narrow)).not.toBe(hashKey(keys.files.childrenOf("d1", "n2")));
    expect(hashKey(narrow)).not.toBe(hashKey(keys.files.childrenOf("d2", "n1")));
  });

  it("the permissions family prefix reaches both readings of a node's grants", () => {
    // The share dialog caches the direct grants and the effective set under two
    // entries; a grant written or revoked has to refresh both, so the prefix an
    // event names must sit above the `effective` flag.
    for (const effective of [false, true]) {
      const one = keys.files.permissions("n1", effective);
      expect(one.slice(0, keys.files.permissionsAll.length)).toEqual([
        ...keys.files.permissionsAll,
      ]);
    }
    expect(hashKey(keys.files.permissionsAll)).not.toBe(hashKey(keys.files.permissions("n1")));
  });

  it("a chat template's own entry sits under the family prefix", () => {
    expect(keys.chatTemplates.one("tpl1").slice(0, keys.chatTemplates.all.length)).toEqual([
      ...keys.chatTemplates.all,
    ]);
  });

  it("the aliases older callers import stay bound to the hoisted tuples", () => {
    expect(meKey).toBe(keys.auth.me);
  });
});

describe("no api module spells a query key inline", () => {
  const apiRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../api");

  function tsFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) return tsFiles(full);
      return name.endsWith(".ts") || name.endsWith(".tsx") ? [full] : [];
    });
  }

  const modules = tsFiles(apiRoot).filter((f) => relative(apiRoot, f) !== "keys.ts");

  // Any array literal handed to react-query as a key — a query's own key, a
  // mutation's `meta.invalidates` list, a cache filter, a cache read/write — and
  // a module-local key constant (`const fooKey = ["…"]`, `const FOO_KEY = ["…"]`,
  // `const teamKey = (id) => ["…", id]`) that would be a second spelling of one.
  const INLINE_KEY = [
    /queryKey:\s*\[/,
    /invalidates:\s*\[\s*\[/,
    /(?:setQueryData|getQueryData|setQueriesData|getQueriesData|cancelQueries|invalidateQueries)(?:<[^(]*>)?\(\s*(?:\{\s*queryKey:\s*)?\[/,
    // The name ends in KEY / Key (or the plural): a `KEYWORDS` list is not a key.
    /const\s+\w*(?:KEY|Key)(?:S|s)?\s*=[^\n]*\[\s*["']/,
  ];

  it("scans the api tree", () => {
    expect(modules.length).toBeGreaterThan(20);
    expect(modules.some((f) => relative(apiRoot, f) === "admin/admin.ts")).toBe(true);
  });

  it.each(modules.map((f) => [relative(apiRoot, f), f] as const))("%s", (_rel, file) => {
    const src = readFileSync(file, "utf8");
    for (const pattern of INLINE_KEY) {
      expect(src.match(pattern)?.[0]).toBeUndefined();
    }
  });
});

describe("teamConnections mutations declare their invalidation through meta", () => {
  const TEAM = "11111111-1111-1111-1111-111111111111";
  const OTHER_TEAM = "33333333-3333-3333-3333-333333333333";
  const CONNECTION = "22222222-2222-2222-2222-222222222222";

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const json = (body: unknown): Response =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });

  type Wrapper = ({ children }: { children: ReactNode }) => ReactElement;

  /** The app's real client (the shared MutationCache policy is what turns
   *  `meta.invalidates` into a refresh — a bare QueryClient would make the meta inert),
   *  with this team's list, ANOTHER team's list and the connector catalog already
   *  cached so the policy has three live entries to choose between. */
  function seeded() {
    const qc = createQueryClient({ retry: false });
    qc.setQueryData(keys.teamConnections.team(TEAM), []);
    qc.setQueryData(keys.teamConnections.team(OTHER_TEAM), []);
    qc.setQueryData(keys.teamConnections.forms, { forms: [] });
    const wrapper: Wrapper = ({ children }) =>
      createElement(QueryClientProvider, { client: qc }, children);
    return { qc, wrapper };
  }

  const UPSERT: TeamConnectionUpsert = {
    plugin: "snowflake",
    handle: "warehouse",
    auth_method: "password",
    auto_add: false,
    enabled: true,
    // A save names the verification it is the consequence of; the server
    // refuses one that doesn't.
    verification_id: "44444444-4444-4444-4444-444444444444",
  };

  const cases: { name: string; mutate: (wrapper: Wrapper) => Promise<unknown> }[] = [
    {
      name: "useUpsertTeamConnection",
      mutate: async (wrapper) => {
        const { result } = renderHook(() => useUpsertTeamConnection(TEAM), { wrapper });
        await result.current.mutateAsync(UPSERT);
      },
    },
    {
      name: "useRotateTeamConnectionSecret",
      mutate: async (wrapper) => {
        const { result } = renderHook(() => useRotateTeamConnectionSecret(TEAM), { wrapper });
        await result.current.mutateAsync({ connectionId: CONNECTION, secret: "s3cret" });
      },
    },
    {
      name: "useDeleteTeamConnection",
      mutate: async (wrapper) => {
        const { result } = renderHook(() => useDeleteTeamConnection(TEAM), { wrapper });
        await result.current.mutateAsync(CONNECTION);
      },
    },
  ];

  it.each(cases)("$name refreshes the team's list and nothing else", async ({ mutate }) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({})));
    const { qc, wrapper } = seeded();

    await mutate(wrapper);

    expect(qc.getQueryState(keys.teamConnections.team(TEAM))?.isInvalidated).toBe(true);
    // Another team's list and the connector catalog (static per deploy) are unrelated to
    // this write: a mutation that declared nothing (the policy's invalidate-all default)
    // would have marked both too.
    expect(qc.getQueryState(keys.teamConnections.team(OTHER_TEAM))?.isInvalidated).toBe(false);
    expect(qc.getQueryState(keys.teamConnections.forms)?.isInvalidated).toBe(false);
  });
});

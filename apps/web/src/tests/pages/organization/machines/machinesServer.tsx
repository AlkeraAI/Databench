// A small stateful stand-in for the machines API, so the pages are driven
// through their real hooks and the real mutation policy: a write changes the
// server's state, and a test that sees the change on screen proves the page
// re-read it rather than patching its own cache.

import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";

import type {
  OrgComputeSettingsRead,
  OrgMachineDetail,
  OrgMachineRead,
  WorkspaceMachineRead,
} from "@/api/machines";
import { createQueryClient } from "@/api/queryClient";
import { TopbarSlotsContext } from "@/app/Topbar";

import { Elsewhere } from "./Elsewhere";

export const ORG_ID = "00000000-0000-4000-8000-0000000000aa";
export const ME = "00000000-0000-4000-8000-000000000001";
export const TEAM_DATA = "00000000-0000-4000-8000-0000000000d1";
export const TEAM_ML = "00000000-0000-4000-8000-0000000000d2";

export function orgMachine(overrides: Partial<OrgMachineRead> = {}): OrgMachineRead {
  const id = overrides.id ?? "00000000-0000-4000-8000-0000000000m1";
  const name = overrides.name ?? "A100 Lab";
  return {
    id,
    name,
    card: {
      kind: "org_machine",
      org_machine_id: id,
      name,
      spec: {
        offering_name: "A100 80 GB",
        provider: "runpod",
        region: "US-TX",
        gpu: { name: "A100", count: 1, memory_gb: 80 },
        vcpu: 16,
        memory_gb: 125,
        disk_gb: 200,
        rate_per_minute_nanos: null,
        storage_rate_per_minute_nanos: null,
      },
      state: "running",
      step: null,
      step_started_at: null,
      step_expected_seconds: null,
      stop_reason: "",
      disk_full: false,
    },
    use_mode: "assigned",
    acquisition: "added",
    free_until: null,
    audience: [{ kind: "team", team_id: TEAM_DATA, user_id: null, label: "Data" }],
    idle_stop_minutes: 30,
    monthly_cap_nanos: null,
    owner_team_id: ORG_ID,
    owner_team_name: "Acme",
    version: 3,
    can_manage: true,
    can_use: true,
    can_replace: true,
    org_default: false,
    spend_this_cycle_nanos: null,
    credit_state: "ok",
    runway_minutes: null,
    created_at: "2026-10-01T10:00:00Z",
    ...overrides,
  };
}

export function detailOf(machine: OrgMachineRead, extra: Partial<OrgMachineDetail> = {}): OrgMachineDetail {
  return { ...machine, workspaces: [], timeline: [], ...extra };
}

export interface Viewer {
  isOrgAdmin: boolean;
  adminTeamIds?: string[];
  /** The server allows the org an org pool. */
  enterprise?: boolean;
  /** The server lets the reader add a machine the org runs. */
  canAdd?: boolean;
}

export interface Sent {
  method: string;
  path: string;
  search: string;
  body: unknown;
  ifMatch: string | null;
}

export type Handler = (body: unknown, req: Request) => Response | Promise<Response>;

export const json = (body: unknown, status = 200): Response =>
  body === undefined
    ? new Response(null, { status, headers: { "content-length": "0" } })
    : new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

export const refuse = (status: number, code: string, message: string): Response =>
  json({ detail: { code, message } }, status);

export interface MachinesServer {
  machines: OrgMachineRead[];
  details: Record<string, Partial<OrgMachineDetail>>;
  /** What a disk quote says of admission: admitted unless a test refuses it. */
  quoteVerdict: { verdict: "ok" | "refused"; code: string | null; message: string };
  /** Whether the deployment prices machines: every quote says so. */
  priced: boolean;
  workspaceMachines: Record<string, WorkspaceMachineRead>;
  /** The org's compute settings; org admins read and change them. */
  computeSettings: OrgComputeSettingsRead;
  viewer: Viewer;
  /** Writes answered by the test instead of the defaults, keyed `METHOD path`. */
  writes: Record<string, Handler>;
  sent: Sent[];
  /** The calls of one method and path. */
  calls: (method: string, path: string) => Sent[];
}

/** Routes a test suite answers before the defaults: `undefined` passes the request on. */
export type Routes<S extends MachinesServer> = (server: S, req: Request, path: string, body: unknown) => Response | undefined | Promise<Response | undefined>;

/** Stand the server up behind `fetch`. Defaults: one running machine the
 *  reader manages, and an org admin. `routes` answers first, for a suite that
 *  serves more than the open machines API. */
export function machinesServer<S extends MachinesServer = MachinesServer>(
  extend?: (server: MachinesServer) => S,
  routes?: Routes<S>,
): S {
  const base: MachinesServer = {
    machines: [orgMachine()],
    details: {},
    quoteVerdict: { verdict: "ok", code: null, message: "" },
    priced: false,
    workspaceMachines: {},
    computeSettings: { shared_pool_fallback: true, min_awake_pool: 0, default_org_machine_id: null, version: 0 },
    viewer: { isOrgAdmin: true, adminTeamIds: [ORG_ID], enterprise: false },
    writes: {},
    sent: [],
    calls: (method, path) => server.sent.filter((s) => s.method === method && s.path === path),
  };
  const server = extend ? extend(base) : (base as S);

  const update = (id: string, change: (m: OrgMachineRead) => OrgMachineRead) => {
    server.machines = server.machines.map((m) => (m.id === id ? change(m) : m));
    return server.machines.find((m) => m.id === id)!;
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      const url = new URL(req.url);
      const path = url.pathname;
      const text = req.method === "GET" || req.method === "DELETE" ? "" : await req.clone().text();
      const body: unknown = text ? JSON.parse(text) : undefined;
      server.sent.push({ method: req.method, path, search: url.search, body, ifMatch: req.headers.get("If-Match") });
      const write = server.writes[`${req.method} ${path}`];
      if (write) return write(body, req);
      const extended = routes ? await routes(server, req, path, body) : undefined;
      if (extended) return extended;

      const v = server.viewer;
      if (path === "/api/v1/dashboard") {
        return json({
          user: { id: ME, email: "me@acme.test", display_name: "Me" },
          org: { id: ORG_ID, name: "Acme", parent_team_id: null, is_root: true, created_at: "2026-01-01T00:00:00Z", member_count: 4 },
          teams: [],
          pending_invitations: [],
          is_org_admin: v.isOrgAdmin,
          entitled_features: [],
          enterprise_features_enabled: v.enterprise === true,
        });
      }
      if (path === "/api/v1/auth/me") {
        return json({ id: ME, email: "me@acme.test", display_name: "Me", admin_team_ids: v.adminTeamIds ?? [] });
      }
      if (path === "/api/v1/teams") {
        return json([
          { id: ORG_ID, name: "Acme", parent_team_id: null, is_root: true, created_at: "2026-01-01T00:00:00Z", member_count: 4 },
          { id: TEAM_DATA, name: "Data", parent_team_id: ORG_ID, is_root: false, created_at: "2026-01-01T00:00:00Z", member_count: 2 },
          { id: TEAM_ML, name: "ML", parent_team_id: TEAM_DATA, is_root: false, created_at: "2026-01-01T00:00:00Z", member_count: 1 },
        ]);
      }
      if (path === "/api/v1/org/members") {
        return json([
          { user_id: ME, email: "me@acme.test", display_name: "Me", is_admin: true, is_active: true, sso_exempt: false },
          { user_id: "u-ada", email: "ada@acme.test", display_name: "Ada Lovelace", is_admin: false, is_active: true, sso_exempt: false },
        ]);
      }
      if (path.startsWith("/api/v1/teams/") && path.endsWith("/members")) {
        return json([{ user_id: "u-grace", display_name: "Grace Hopper", email: "grace@acme.test", team_id: TEAM_DATA, role: "member" }]);
      }
      if (path === "/api/v1/org/machines/buying" && req.method === "GET") {
        return json({ can_buy: false, reason: null, quota: 0, used: server.machines.length, can_add: v.canAdd === true });
      }
      if (path === "/api/v1/org/compute/settings") {
        if (!v.isOrgAdmin) return json({ detail: "Only an org admin can change the organization's compute settings." }, 403);
        if (req.method === "GET") return json(server.computeSettings);
        if (req.headers.get("If-Match") !== String(server.computeSettings.version)) {
          return refuse(409, "stale_write", "This setting changed since you loaded it.");
        }
        const patch = body as { default_org_machine_id?: string | null };
        const next = patch.default_org_machine_id;
        if (next != null && !server.machines.some((m) => m.id === next)) return refuse(404, "not_found", "Machine not found");
        server.computeSettings = {
          ...server.computeSettings,
          ...(next !== undefined ? { default_org_machine_id: next } : {}),
          version: server.computeSettings.version + 1,
        };
        return json(server.computeSettings);
      }
      if (path === "/api/v1/org/machines" && req.method === "GET") {
        // The list marks the default the way the server reads it from the settings.
        const marked = server.computeSettings.default_org_machine_id;
        return json(server.machines.map((m) => ({ ...m, org_default: m.id === marked })));
      }
      const disk = path.match(/^\/api\/v1\/org\/machines\/([^/]+)\/disk(\/quote)?$/);
      if (disk && req.method === "POST") {
        // The server's grow: refused outside the sizes it offered, priced at
        // 1,000 nanos a GB-minute, and on a grow the disk becomes the size.
        const [, id, quoting] = disk;
        const machine = server.machines.find((m) => m.id === id);
        if (!machine?.disk_grow) return refuse(409, "disk_cannot_grow", "This machine's disk can't grow in place.");
        const gb = (body as { volume_gb: number }).volume_gb;
        const { min_gb: low, max_gb: high } = machine.disk_grow;
        if (gb < low || gb > high) return refuse(422, "storage_out_of_range", `Storage must be between ${low} and ${high} GB.`);
        if (quoting) {
          return json({
            offering_id: "o-1",
            storage_gb: gb,
            rate_per_minute_nanos: 0,
            storage_rate_per_minute_nanos: gb * 1_000,
            storage_per_month_nanos: gb * 1_000 * 43_200,
            volume_billed_while_stopped: true,
            start_runway_nanos: 0,
            priced: server.priced,
            ...server.quoteVerdict,
          });
        }
        const grown = update(id, (m) => ({
          ...m,
          version: m.version + 1,
          disk_grow: { ...machine.disk_grow!, min_gb: gb + 1 },
          card: { ...m.card, spec: m.card.spec ? { ...m.card.spec, disk_gb: gb } : null },
        }));
        return json(grown, 202);
      }
      const one = path.match(/^\/api\/v1\/org\/machines\/([^/]+)(?:\/(start|stop|replace|audience))?$/);
      if (one) {
        const [, id, action] = one;
        const machine = server.machines.find((m) => m.id === id);
        if (!machine) return json({ detail: "Not found" }, 404);
        if (req.method === "GET" && !action) return json(detailOf(machine, server.details[id]));
        const stale = req.headers.get("If-Match") !== null && req.headers.get("If-Match") !== String(machine.version);
        if (stale) return refuse(409, "stale_write", "This machine changed since you loaded it.");
        const bump = (m: OrgMachineRead): OrgMachineRead => ({ ...m, version: m.version + 1 });
        if (req.method === "PATCH") {
          const patch = body as Partial<OrgMachineRead>;
          return json(update(id, (m) => bump({ ...m, ...patch, card: { ...m.card, name: patch.name ?? m.card.name } })));
        }
        if (req.method === "PUT" && action === "audience") {
          const grants = (body as { audience: { kind: string; team_id?: string; user_id?: string }[] }).audience;
          return json(update(id, (m) => bump({ ...m, audience: grants.map((g) => ({ ...g, label: g.team_id ?? g.user_id ?? "Everyone" })) as OrgMachineRead["audience"] })));
        }
        if (req.method === "POST" && action === "start") return json(update(id, (m) => bump({ ...m, card: { ...m.card, state: "starting", step: "reserving" } })), 202);
        if (req.method === "POST" && action === "stop") return json(update(id, (m) => bump({ ...m, card: { ...m.card, state: "stopping" } })), 202);
        if (req.method === "POST" && action === "replace") return json(update(id, (m) => bump({ ...m, card: { ...m.card, state: "starting", step: "reserving" } })), 202);
        if (req.method === "DELETE") {
          server.machines = server.machines.filter((m) => m.id !== id);
          // Deleting the default machine clears the default.
          if (server.computeSettings.default_org_machine_id === id) {
            server.computeSettings = {
              ...server.computeSettings,
              default_org_machine_id: null,
              version: server.computeSettings.version + 1,
            };
          }
          return json(undefined, 202);
        }
      }
      const ws = path.match(/^\/api\/v1\/workspaces\/([^/]+)\/machine$/);
      if (ws && req.method === "GET") {
        const read = server.workspaceMachines[ws[1]];
        return read ? json(read) : json({ detail: "Not found" }, 404);
      }
      return json({ detail: `unrouted ${req.method} ${path}` }, 404);
    }),
  );
  return server;
}

/** Render `element` at `path` with a fresh client and a masthead actions slot. */
export function renderAt(element: ReactElement, path: string, route = path) {
  const actions = document.createElement("div");
  actions.id = "test-topbar-actions";
  document.body.appendChild(actions);
  const qc = createQueryClient({ retry: false });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <TopbarSlotsContext.Provider
          value={{ subtitle: null, actions, framed: true, setTitleHidden: () => {}, setTopbarHidden: () => {} }}
        >
          <Routes>
            <Route path={route} element={element} />
            <Route path="*" element={<Elsewhere />} />
          </Routes>
        </TopbarSlotsContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, qc, actions };
}

export function removeTopbar(): void {
  document.getElementById("test-topbar-actions")?.remove();
}

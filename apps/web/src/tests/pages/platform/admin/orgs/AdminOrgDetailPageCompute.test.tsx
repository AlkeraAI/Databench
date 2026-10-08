import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@alkera/sdk";

import { meKey } from "@/api/auth";
import { createQueryClient } from "@/api/queryClient";
import { keys } from "@/api/keys";
import {
  AdminOrgDetailPage,
  COMPUTE_LABELS,
  TAB_LABELS,
  revokeConfirmTitle,
} from "@/pages/platform/admin/orgs/AdminOrgDetailPage";
import { computeExpiry } from "@/pages/platform/admin/shared/modals";

// The Compute band on the org-detail register — the operator act that replaced the provisioning
// script. Driven through the REAL hooks and the REAL query client (createQueryClient, so the global
// MutationCache invalidation policy is live and the grant's `meta.invalidates` actually refreshes the
// list). `fetch` is stubbed; every searched string comes from the component's own exported constants.

type Org = components["schemas"]["OrgRead"];
type User = components["schemas"]["UserRead"];
type Grant = components["schemas"]["ComputeGrantRead"];

const ORG_ID = "org-under-test";

const ORG: Org = {
  name: "Tideline Analytics",
  id: ORG_ID,
  parent_team_id: null,
  is_root: true,
  created_at: "2026-01-01T00:00:00Z",
  member_count: 3,
};

const GRANT: Grant = {
  id: "grant-1",
  org_team_id: ORG_ID,
  team_name: "Tideline Analytics",
  machine_type_id: null,
  machine_type: "any",
  machine_type_display_name: "Any machine type",
  ceiling: 2,
  per_user_max: null,
  rate_per_minute_nanos: 0,
  expires_at: "2026-12-01T00:00:00Z",
  note: "onboarding",
  created_at: "2026-09-01T00:00:00Z",
};

const staff = (role: User["platform_role"]): User => ({
  email: "staff@example.com",
  first_name: "Sam",
  last_name: "Staff",
  id: "u-staff",
  org_team_id: "t-root",
  org_name: "Tideline",
  org_role: "member",
  membership_count: 1,
  display_name: "Sam Staff",
  platform_role: role,
  email_verification_required: false,
  has_password: true,
  mfa_enabled: false,
  created_at: "2026-01-01T00:00:00Z",
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** Render the register with the viewer's grade and the org's grants seeded, and return both the client
 *  and the recorded fetch calls so a test can assert the exact request the UI sent. */
function renderCompute({ role, grants }: { role: User["platform_role"]; grants: Grant[] }) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  // The list is served by fetch (not seeded) so the refetch a mutation triggers is observable: the
  // second GET is what proves `meta.invalidates` reached this key.
  let served = grants;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // openapi-fetch hands `fetch` a Request object, not (url, init) — read both shapes.
      const req = input instanceof Request ? input : null;
      const url = req ? req.url : String(input);
      const method = (req ? req.method : init?.method) ?? "GET";
      const raw = req ? await req.clone().text() : init?.body ? String(init.body) : "";
      calls.push({ url, method, body: raw ? JSON.parse(raw) : null });
      if (method === "PUT") {
        served = [{ ...GRANT, id: "grant-new", ceiling: 5 }];
        return new Response(JSON.stringify(served[0]), { status: 201, headers: { "content-type": "application/json" } });
      }
      if (method === "DELETE") {
        served = [];
        return new Response(null, { status: 204 });
      }
      if (!url.includes("/compute")) {
        // Every other read the register makes is seeded below; an unexpected call must be loud
        // rather than quietly answered with grant-shaped data.
        return new Response(JSON.stringify({ detail: `unstubbed ${url}` }), { status: 500 });
      }
      return new Response(JSON.stringify(served), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );

  const qc = createQueryClient({ retry: false });
  qc.setQueryData(meKey, staff(role));
  qc.setQueryData(keys.admin.org(ORG_ID), ORG);
  qc.setQueryData(keys.admin.orgMembers(ORG_ID), []);
  // The sibling bands on the billing tab read their own endpoints; seed them so this test exercises
  // only the compute band.
  qc.setQueryData(keys.admin.enterpriseOrgs, []);
  qc.setQueryData(keys.admin.billingOrg(ORG_ID), { pool_balances: [], team_pools: [], members: [] });
  qc.setQueryData(keys.admin.recurringGrantsOrg(ORG_ID), []);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/admin/orgs/${ORG_ID}`]}>
        <Routes>
          <Route path="/admin/orgs/:orgId" element={<AdminOrgDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { qc, calls };
}

const openBilling = () => fireEvent.click(screen.getByRole("tab", { name: TAB_LABELS.overview }));

/** The grants table. With the billing bands seeded empty it is the only table on the tab, so scoping
 *  a figure to it can never match a neighbouring band's number. */
const grantsTable = () => screen.findByRole("table");

describe("AdminOrgDetailPage — compute grants", () => {
  it("names the refusal an operator would otherwise have to debug when the org has no grant", async () => {
    renderCompute({ role: "alkera_admin", grants: [] });
    openBilling();
    expect(await screen.findByText(COMPUTE_LABELS.empty)).toBeInTheDocument();
  });

  it("lists a live grant's ceiling next to the machine type it admits", async () => {
    renderCompute({ role: "alkera_admin", grants: [GRANT] });
    openBilling();
    const table = await grantsTable();
    // A wildcard grant has no machine type, so the band says so rather than showing an empty cell.
    expect(within(table).getByText(COMPUTE_LABELS.anyType)).toBeInTheDocument();
    expect(within(table).getByText(String(GRANT.ceiling))).toBeInTheDocument();
    expect(screen.queryByText(COMPUTE_LABELS.empty)).not.toBeInTheDocument();
  });

  it("tells a wildcard grant apart from a typed one, and never guesses from a blank name", async () => {
    // The two facts are opposites: a wildcard grant admits EVERY machine type (a GPU included),
    // a typed grant exactly one. The band reads the machine type id to tell them apart — reading a
    // blank display name as "any" would widen the most expensive grant we issue on the very screen
    // an operator reviews it on.
    const typed: Grant = {
      ...GRANT,
      id: "grant-typed",
      machine_type_id: "mt-1",
      machine_type: "cpu3c",
      machine_type_display_name: "CPU 3-core",
    };
    const nameless: Grant = { ...typed, id: "grant-nameless", machine_type_display_name: "" };
    renderCompute({ role: "alkera_admin", grants: [GRANT, typed, nameless] });
    openBilling();
    const table = await grantsTable();

    expect(within(table).getAllByText(COMPUTE_LABELS.anyType)).toHaveLength(1);
    expect(within(table).getByText("CPU 3-core")).toBeInTheDocument();
    // A typed grant with no display name still names its own type, never the wildcard.
    expect(within(table).getByText("cpu3c")).toBeInTheDocument();
  });

  it("hides the grant and revoke controls from a support-grade staffer and says why", async () => {
    renderCompute({ role: "alkera_support", grants: [GRANT] });
    openBilling();
    await grantsTable();
    expect(screen.queryByRole("button", { name: COMPUTE_LABELS.grant })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COMPUTE_LABELS.revoke })).not.toBeInTheDocument();
    expect(screen.getByText(COMPUTE_LABELS.adminOnly)).toBeInTheDocument();
  });

  it("sends the ceiling and a future expiry, then refreshes the list from the server", async () => {
    const { calls } = renderCompute({ role: "alkera_admin", grants: [] });
    openBilling();
    await screen.findByText(COMPUTE_LABELS.empty);

    fireEvent.click(screen.getByRole("button", { name: COMPUTE_LABELS.grant }));
    const machines = await screen.findByLabelText(/concurrent machines/i);
    fireEvent.change(machines, { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Grant" }));

    const put = await waitFor(() => {
      const found = calls.find((c) => c.method === "PUT");
      expect(found).toBeDefined();
      return found!;
    });
    expect(put.url).toContain(`/admin/v1/orgs/${ORG_ID}/compute`);
    const sent = put.body as { ceiling: number; expires_at: string };
    expect(sent.ceiling).toBe(5);
    // Every grant expires; the form must never send a lapsed or absent one.
    expect(new Date(sent.expires_at).getTime()).toBeGreaterThan(Date.now());

    // The mutation declares meta.invalidates for this org's compute key, so the list refetches and the
    // band shows the new ceiling without the page being reloaded.
    await waitFor(() => {
      expect(calls.filter((c) => c.method === "GET" && c.url.includes("/compute")).length).toBeGreaterThan(1);
    });
    expect(within(await grantsTable()).getByText("5")).toBeInTheDocument();
  });

  it("confirms before revoking, names the grant, and drops it from the band", async () => {
    const { calls } = renderCompute({ role: "alkera_admin", grants: [GRANT] });
    openBilling();
    await grantsTable();

    fireEvent.click(screen.getByRole("button", { name: COMPUTE_LABELS.revoke }));
    // The confirmation names WHICH grant is going — a wildcard grant is the whole org's compute.
    await screen.findByText(revokeConfirmTitle(COMPUTE_LABELS.anyType));
    // Nothing has been sent yet: the confirm step is the guard, not a formality.
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);

    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: COMPUTE_LABELS.revoke }));

    const del = await waitFor(() => {
      const found = calls.find((c) => c.method === "DELETE");
      expect(found).toBeDefined();
      return found!;
    });
    expect(del.url).toContain(`/admin/v1/orgs/${ORG_ID}/compute/${GRANT.id}`);
    expect(await screen.findByText(COMPUTE_LABELS.empty)).toBeInTheDocument();
  });

  it("backing out of the revoke keeps the grant and sends nothing", async () => {
    const { calls } = renderCompute({ role: "alkera_admin", grants: [GRANT] });
    openBilling();
    await grantsTable();

    fireEvent.click(screen.getByRole("button", { name: COMPUTE_LABELS.revoke }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    expect(within(await grantsTable()).getByRole("button", { name: COMPUTE_LABELS.revoke })).toBeInTheDocument();
  });
});

describe("computeExpiry", () => {
  it("turns a window in days into the instant the API takes", () => {
    const now = new Date("2026-09-01T00:00:00.000Z");
    expect(computeExpiry(30, now)).toBe("2026-10-01T00:00:00.000Z");
  });
});

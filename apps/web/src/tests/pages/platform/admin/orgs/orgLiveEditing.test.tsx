import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@alkera/sdk";

import { meKey } from "@/api/auth";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { AdminOrgDetailPage, TAB_LABELS } from "@/pages/platform/admin/orgs/AdminOrgDetailPage";
import { LIVE_EDITING_LABELS } from "@/pages/platform/admin/orgs/LiveEditingCard";

// The live-editing band on the org-detail register: the deployment's default,
// the org's own setting with its three choices, a confirmation before any
// change, and the route's answer afterwards. Driven through the real page and
// hooks with only `fetch` stubbed, so the request on the wire is what is pinned.

type Org = components["schemas"]["OrgRead"];
type User = components["schemas"]["UserRead"];
type Live = components["schemas"]["OrgLiveEditingRead"];

const ORG_ID = "org-under-test";

const ORG: Org = {
  name: "Tideline Analytics",
  id: ORG_ID,
  parent_team_id: null,
  is_root: true,
  created_at: "2026-01-01T00:00:00Z",
  member_count: 3,
};

const following: Live = {
  org_id: ORG_ID,
  enabled: true,
  override: null,
  deployment_default: true,
  sessions_written: null,
  sessions_left_unsaved: null,
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

type Call = { url: string; method: string; body: unknown };

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderLive({
  role = "alkera_admin" as User["platform_role"],
  live = following,
  written = { sessions_written: 2, sessions_left_unsaved: 0 },
}: {
  role?: User["platform_role"];
  live?: Live;
  written?: { sessions_written: number | null; sessions_left_unsaved: number | null };
} = {}) {
  const calls: Call[] = [];
  let served = live;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const req = input instanceof Request ? input : null;
      const url = req ? req.url : String(input);
      const method = (req ? req.method : init?.method) ?? "GET";
      const raw = req ? await req.clone().text() : init?.body ? String(init.body) : "";
      calls.push({ url, method, body: raw ? JSON.parse(raw) : null });
      if (!url.includes("/live-editing")) {
        return new Response(JSON.stringify({ detail: `unstubbed ${url}` }), { status: 500 });
      }
      if (method === "PUT") {
        const enabled = (calls[calls.length - 1].body as { enabled: boolean | null }).enabled;
        served = { ...served, override: enabled, enabled: enabled ?? served.deployment_default };
        const answer = enabled === false || (enabled === null && !served.deployment_default) ? written : {};
        return new Response(JSON.stringify({ ...served, ...answer }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify(served), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );

  const qc = createQueryClient({ retry: false });
  qc.setQueryData(meKey, staff(role));
  qc.setQueryData(keys.admin.org(ORG_ID), ORG);
  qc.setQueryData(keys.admin.orgMembers(ORG_ID), []);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/admin/orgs/${ORG_ID}`]}>
        <Routes>
          <Route path="/admin/orgs/:orgId" element={<AdminOrgDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("tab", { name: TAB_LABELS.overview }));
  return { calls };
}

async function liveCard(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: LIVE_EDITING_LABELS.title });
  return heading.closest(".alk-card") as HTMLElement;
}

const writes = (calls: Call[]) => calls.filter((c) => c.method === "PUT" && c.url.includes("/live-editing"));

async function choose(label: string) {
  const card = await liveCard();
  const group = within(card).getByRole("radiogroup", { name: LIVE_EDITING_LABELS.control });
  fireEvent.click(within(group).getByRole("radio", { name: label }));
}

describe("Admin org detail — the org's live editing", () => {
  it.each([
    [{ ...following }, "On", "Follow deployment", "On"],
    [{ ...following, override: false, enabled: false }, "On", "Off", "Off"],
    [{ ...following, deployment_default: false, enabled: true, override: true }, "Off", "On", "On"],
  ])("reads the deployment default, the org's setting and the result (%#)", async (live, deployment, org, result) => {
    renderLive({ live });
    const card = await liveCard();
    const rows = within(card);
    expect(rows.getByText(LIVE_EDITING_LABELS.deployment).nextElementSibling?.textContent).toBe(deployment);
    expect(rows.getByText(LIVE_EDITING_LABELS.orgSetting).nextElementSibling?.textContent).toBe(org);
    expect(rows.getByText(LIVE_EDITING_LABELS.result).nextElementSibling?.textContent).toBe(result);
  });

  it("writes nothing until the confirmation is answered", async () => {
    const { calls } = renderLive();
    await choose(LIVE_EDITING_LABELS.off);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Live editing ends for everyone in this org/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(writes(calls)).toEqual([]);
  });

  it.each([
    ["Off", { override: null }, false],
    ["On", { override: false, enabled: false }, true],
    ["Follow deployment", { override: true }, null],
  ] as const)("choosing %s sends that setting", async (label, start, body) => {
    const { calls } = renderLive({ live: { ...following, ...start } as Live });
    await choose(label);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: LIVE_EDITING_LABELS.confirm }));
    await waitFor(() => expect(writes(calls)).toHaveLength(1));
    expect(writes(calls)[0].body).toEqual({ enabled: body });
  });

  it("shows how many sessions were saved and how many were left unsaved", async () => {
    renderLive({ written: { sessions_written: 3, sessions_left_unsaved: 1 } });
    await choose(LIVE_EDITING_LABELS.off);
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: LIVE_EDITING_LABELS.confirm }));
    expect(await screen.findByText("3 sessions saved; 1 left unsaved for the background sweep.")).toBeInTheDocument();
    const card = await liveCard();
    await waitFor(() =>
      expect(within(card).getByText(LIVE_EDITING_LABELS.result).nextElementSibling?.textContent).toBe("Off"),
    );
  });

  it("lets support staff read it but not change it", async () => {
    const { calls } = renderLive({ role: "alkera_support" });
    const card = await liveCard();
    expect(within(card).getByText(LIVE_EDITING_LABELS.adminOnly)).toBeInTheDocument();
    expect(within(card).queryByRole("radiogroup")).not.toBeInTheDocument();
    expect(writes(calls)).toEqual([]);
  });
});

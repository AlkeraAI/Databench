import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MemberDrawer } from "@/pages/organization/teams/overlays/MemberDrawer";

// The member drawer driven through REAL hooks with only `fetch` (the network boundary) stubbed, so
// the wire contract is pinned: an ORG admin gets the account controls, and each action hits the right
// endpoint with the right body. Deactivation is destructive, so it must confirm FIRST — the endpoint
// fires only from the confirm button. Budget and storage limits are not here: they live on the
// team's Allocations tab, so no viewer gets a limit control in the drawer.

const target = { teamId: "t1", teamName: "Platform", person: { id: "u1", name: "Marcus", email: "m@x.io", initials: "MB" } };

const state = {
  isOrgAdmin: true,
  enterpriseEnabled: true,
  plan: { status: 200, body: { tier_key: "pro", tier_name: "Pro", pct_used: 42, reset_at: null } as unknown },
  member: { user_id: "u1", email: "m@x.io", display_name: "Marcus", is_admin: false, is_active: true, sso_exempt: false },
};

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function route(req: Request): Response {
  const p = new URL(req.url).pathname;
  if (req.method === "GET") {
    if (p === "/api/v1/dashboard")
      return json({
        user: {},
        org: {},
        teams: [],
        pending_invitations: [],
        is_org_admin: state.isOrgAdmin,
        enterprise_features_enabled: state.enterpriseEnabled,
      });
    if (p === "/api/v1/teams/t1/memberships/u1/plan") return json(state.plan.body, state.plan.status);
    if (p === "/api/v1/org/members") return json([state.member]);
  }
  if (req.method === "PUT" && p === "/api/v1/org/members/u1/active") return json(state.member);
  if (req.method === "PUT" && p === "/api/v1/org/sso/exemptions/u1") return json(state.member);
  return json({ detail: `unmatched ${req.method} ${p}` }, 404);
}

let fetchSpy: ReturnType<typeof vi.fn>;

/** The stubbed requests that hit `method path`, each with its JSON body parsed. */
function requestsTo(method: string, path: string): Promise<unknown[]> {
  const hits = fetchSpy.mock.calls
    .map((c) => c[0] as Request)
    .filter((r) => r.method === method && new URL(r.url).pathname === path);
  return Promise.all(hits.map((r) => r.clone().json().catch(() => undefined)));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
beforeEach(() => {
  state.isOrgAdmin = true;
  state.enterpriseEnabled = true;
  state.plan = { status: 200, body: { tier_key: "pro", tier_name: "Pro", pct_used: 42, reset_at: null } };
  state.member = { user_id: "u1", email: "m@x.io", display_name: "Marcus", is_admin: false, is_active: true, sso_exempt: false };
  fetchSpy = vi.fn(async (input: Request | string, init?: RequestInit) =>
    route(input instanceof Request ? input : new Request(input, init)),
  );
  vi.stubGlobal("fetch", fetchSpy);
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const notify = vi.fn();
async function renderDrawer(t: typeof target | null = target) {
  notify.mockClear();
  render(<MemberDrawer target={t} onClose={() => {}} onNotify={notify} />, { wrapper });
  // The SidePanel's focus trap moves initial focus on the next animation frame; let it land before
  // interacting so a keystroke doesn't race it (a user can't type within one frame of open anyway).
  await act(() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))));
}

describe("MemberDrawer with no section installed", () => {
  it("renders nothing actionable when closed (no target)", async () => {
    await renderDrawer(null);
    expect(screen.queryByText(/current plan/i)).not.toBeInTheDocument();
  });

  it("shows the member and asks for no plan", async () => {
    await renderDrawer();
    expect(await screen.findByRole("checkbox", { name: "Active" })).toBeInTheDocument();
    expect(screen.queryByText(/current plan/i)).not.toBeInTheDocument();
    expect(await requestsTo("GET", "/api/v1/teams/t1/memberships/u1/plan")).toHaveLength(0);
  });
});

/** Every limit control the drawer must not carry: a budget or storage field, and their actions. */
function limitControls(): HTMLElement[] {
  return [
    ...screen.queryAllByRole("textbox", { name: /budget|storage|top-up/i }),
    ...screen.queryAllByRole("button", { name: /^(save|remove|top up|clear)$/i }),
  ];
}

describe("MemberDrawer — limits moved to the Allocations tab", () => {
  it("an org admin gets the account controls and no budget or storage controls", async () => {
    await renderDrawer();
    expect(await screen.findByRole("checkbox", { name: "Active" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "SSO break-glass" })).not.toBeChecked();
    expect(limitControls()).toEqual([]);
    expect(await requestsTo("GET", "/api/v1/org/billing")).toHaveLength(0);
  });

  it("a team admin gets nothing to edit — no limits, no account controls", async () => {
    state.isOrgAdmin = false;
    await renderDrawer();
    expect((await screen.findAllByText("Marcus")).length).toBeGreaterThan(0);
    expect(limitControls()).toEqual([]);
    expect(screen.queryByRole("checkbox", { name: "Active" })).not.toBeInTheDocument();
    expect(await requestsTo("GET", "/api/v1/org/members")).toHaveLength(0);
  });
});

describe("MemberDrawer — deactivation + SSO break-glass", () => {
  it("deactivating confirms FIRST, and only the confirm button fires PUT active:false", async () => {
    const user = userEvent.setup();
    await renderDrawer();
    await user.click(await screen.findByRole("checkbox", { name: "Active" }));

    // The confirm gate: the dialog is up and NOTHING has been sent yet.
    expect(await screen.findByRole("dialog", { name: /deactivate marcus/i })).toBeInTheDocument();
    expect(await requestsTo("PUT", "/api/v1/org/members/u1/active")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Deactivate" }));
    await waitFor(async () => expect(await requestsTo("PUT", "/api/v1/org/members/u1/active")).toHaveLength(1));
    expect((await requestsTo("PUT", "/api/v1/org/members/u1/active"))[0]).toEqual({ active: false });
  });

  it("cancelling the confirm sends nothing", async () => {
    const user = userEvent.setup();
    await renderDrawer();
    await user.click(await screen.findByRole("checkbox", { name: "Active" }));
    await screen.findByRole("dialog", { name: /deactivate marcus/i });
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await requestsTo("PUT", "/api/v1/org/members/u1/active")).toHaveLength(0);
  });

  it("REACTIVATING fires immediately — no confirm for the non-destructive direction", async () => {
    state.member = { ...state.member, is_active: false };
    const user = userEvent.setup();
    await renderDrawer();
    const active = await screen.findByRole("checkbox", { name: "Active" });
    expect(active).not.toBeChecked();
    await user.click(active);
    expect(screen.queryByRole("dialog", { name: /deactivate/i })).not.toBeInTheDocument();
    await waitFor(async () => expect(await requestsTo("PUT", "/api/v1/org/members/u1/active")).toHaveLength(1));
    expect((await requestsTo("PUT", "/api/v1/org/members/u1/active"))[0]).toEqual({ active: true });
  });

  it("toggling SSO break-glass PUTs the exemption", async () => {
    const user = userEvent.setup();
    await renderDrawer();
    await user.click(await screen.findByRole("checkbox", { name: "SSO break-glass" }));
    await waitFor(async () => expect(await requestsTo("PUT", "/api/v1/org/sso/exemptions/u1")).toHaveLength(1));
    expect((await requestsTo("PUT", "/api/v1/org/sso/exemptions/u1"))[0]).toEqual({ exempt: true });
  });

  it("enabled break-glass carries no upsell tooltip", async () => {
    const user = userEvent.setup();
    await renderDrawer();
    const toggle = await screen.findByRole("checkbox", { name: "SSO break-glass" });
    expect(toggle).toBeEnabled();
    await user.hover(screen.getByText("SSO break-glass"));
    // Outwait the Tooltip's 300ms open delay — an instant query would pass even
    // if the gated Tooltip wrapper regressed onto the entitled branch.
    await new Promise((r) => setTimeout(r, 450));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
});

describe("MemberDrawer — SSO break-glass on a non-Enterprise org", () => {
  beforeEach(() => {
    state.enterpriseEnabled = false;
  });

  it("greys out the switch and clicking it sends NO exemption PUT", async () => {
    const user = userEvent.setup();
    await renderDrawer();
    const toggle = await screen.findByRole("checkbox", { name: "SSO break-glass" });
    expect(toggle).toBeDisabled();
    await user.click(screen.getByText("SSO break-glass")); // the label is still hoverable/clickable
    expect(await requestsTo("PUT", "/api/v1/org/sso/exemptions/u1")).toHaveLength(0);
  });

  it("hovering the greyed switch explains it's an Enterprise feature", async () => {
    const user = userEvent.setup();
    await renderDrawer();
    await screen.findByRole("checkbox", { name: "SSO break-glass" });
    await user.hover(screen.getByText("SSO break-glass"));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Available on Enterprise plan");
  });

  it("the Active switch stays usable — the gate is break-glass-specific", async () => {
    await renderDrawer();
    expect(await screen.findByRole("checkbox", { name: "Active" })).toBeEnabled();
  });
});

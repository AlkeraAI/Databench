// Regression: changing a team member's role must update the roster in place — the row
// re-renders with the new role without a manual reload. The role mutation
// (api/teams.ts useChangeRoleMutation) declares NOTHING: the refresh is entirely the
// shared MutationCache policy's doing (invalidate-all → the roster queries refetch →
// the teams seam re-derives the graph → the row moves), so this test fails if the
// policy stops invalidating after a mutation. Driven through the REAL route table
// (AppContent) with only `fetch` stubbed, exactly like TeamsPage.test.tsx.

import { MemoryRouter } from "react-router-dom";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppContent } from "@/App";
import { queryClient } from "@/api/queryClient";
import { resetTeamsStore } from "@/pages/organization/teams/data/provider";

const VIEWER = {
  id: "viewer",
  email: "vera@x.io",
  first_name: "Vera",
  last_name: "Ng",
  display_name: "Vera Ng",
  email_verified_at: "2026-01-01T00:00:00Z",
  email_verification_required: false,
  email_verification_deadline: null,
  has_password: true,
  // The org admin: admin of the root, and by descent of every team.
  admin_team_ids: ["root", "platform"],
};

const TEAMS = [
  { id: "root", name: "Tideline", parent_team_id: null, is_root: true, created_at: "2026-01-01T00:00:00Z", member_count: 2 },
  { id: "platform", name: "Platform", parent_team_id: "root", is_root: false, created_at: "2026-01-01T00:00:00Z", member_count: 1 },
];

const memberRow = (user: string, teamId: string, teamName: string, role: string) => ({
  user_id: user,
  display_name: user[0].toUpperCase() + user.slice(1),
  email: `${user}@x.io`,
  first_name: user,
  last_name: "X",
  role,
  team_id: teamId,
  team_name: teamName,
  created_at: "2026-02-01T00:00:00Z",
  role_display: role === "admin" ? "Admin" : "Member",
  effective_role: role,
  direct_role: role,
  descent_role: null,
  descent_from_team_id: null,
  descent_from_team_name: null,
});

// Mutable server state: the PATCH flips the stored role, so the post-mutation
// refetch observes the change exactly like the real backend.
let roles: Record<string, string>;
const rosters = (): Record<string, unknown[]> => ({
  root: [memberRow("viewer", "root", "Tideline", roles.viewer), memberRow("marcus", "platform", "Platform", roles.marcus)],
  platform: [memberRow("marcus", "platform", "Platform", roles.marcus)],
});

const DASH = { user: VIEWER, org: TEAMS[0], teams: TEAMS, pending_invitations: [], is_org_admin: false };
const CREDITS = { tier_key: "pro", tier_name: "Pro", pct_used: 10, reset_at: null, prepaid_credits: 0 };

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function route(req: Request): Response {
  const p = new URL(req.url).pathname;
  if (p === "/api/v1/auth/me") return json(VIEWER);
  if (p === "/api/v1/teams") return json(TEAMS);
  if (p === "/api/v1/dashboard") return json(DASH);
  if (p === "/api/v1/me/credits") return json(CREDITS);
  if (p === "/api/v1/invitations/me") return json([]);
  const roleChange = p.match(/^\/api\/v1\/teams\/([^/]+)\/memberships\/([^/]+)$/);
  if (roleChange && req.method === "PATCH") {
    // The role flip is applied to SERVER state only — nothing pushes it at the UI.
    roles[roleChange[2]] = "admin";
    return json(memberRow(roleChange[2], roleChange[1], "Platform", "admin"));
  }
  const members = p.match(/^\/api\/v1\/teams\/([^/]+)\/members$/);
  if (members) return json(rosters()[members[1]] ?? []);
  if (/^\/api\/v1\/teams\/[^/]+\/invitations$/.test(p)) return json([]);
  // The team page now carries a Preconfigured Connections section, so it fetches
  // these lanes. Answer them (empty) so an unmocked 404 + its retry ladder can't
  // stall the mutation policy's awaited invalidate-all (and delay the toast).
  if (p === "/api/v1/plugins/connection-forms") return json({ connectors: [] });
  if (/^\/api\/v1\/teams\/[^/]+\/connections$/.test(p)) return json([]);
  return json({ detail: `unmatched ${req.method} ${p}` }, 404);
}

let fetchSpy: ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
beforeEach(() => {
  queryClient.clear();
  resetTeamsStore();
  roles = { viewer: "admin", marcus: "member" };
  fetchSpy = vi.fn(async (input: Request | string, init?: RequestInit) =>
    route(input instanceof Request ? input : new Request(input, init)),
  );
  vi.stubGlobal("fetch", fetchSpy);
});

describe("Teams — changing a member's role updates the roster", () => {
  it("the row re-renders with the new role and the toast fires after the refetch", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/teams/platform"]}>
        <AppContent />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 })).toBeInTheDocument();

    // Marcus is a direct member — his row carries the inline role dropdown, labelled with the current role.
    const trigger = await screen.findByRole("button", { name: "Role for Marcus: Member" }, { timeout: 5000 });
    await user.click(trigger);
    await user.click(await screen.findByRole("menuitemradio", { name: "Admin" }));

    // The roster reflects the change because the policy refetched it — the trigger's
    // accessible name now carries the NEW role, and the success toast fired.
    expect(await screen.findByRole("button", { name: "Role for Marcus: Admin" }, { timeout: 5000 })).toBeInTheDocument();
    expect(await screen.findByText(/updated marcus.s role to admin/i)).toBeInTheDocument();

    // And it got there via a server round-trip (PATCH then a fresh roster GET), not a local patch.
    const patches = fetchSpy.mock.calls
      .map((c) => c[0] as Request)
      .filter((r) => r.method === "PATCH" && new URL(r.url).pathname === "/api/v1/teams/platform/memberships/marcus");
    expect(patches).toHaveLength(1);
    const rosterGets = fetchSpy.mock.calls
      .map((c) => c[0] as Request)
      .filter((r) => r.method === "GET" && new URL(r.url).pathname === "/api/v1/teams/platform/members");
    expect(rosterGets.length).toBeGreaterThanOrEqual(2);
  });
});

import { MemoryRouter } from "react-router-dom";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppContent } from "@/App";
import { queryClient } from "@/api/queryClient";
import { resetTeamsStore } from "@/pages/organization/teams/data/provider";

// The Teams page's DEEP LINKS, driven through the REAL route table (AppContent) with only `fetch`
// stubbed — so these fail if the /teams/:teamId route is missing from App.tsx, if the param stops
// seeding the selection, or if ?tab=invites stops opening the invitations panel. Invitation emails
// land on /dashboard/invites → /teams?tab=invites, and the old per-team links land on
// /dashboard/teams/:teamId → /teams/:teamId, so the two legacy entries are pinned end-to-end too.

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

const ROSTERS: Record<string, unknown[]> = {
  // `dana` is a DIRECT member of the org root and is not the viewer, so she is the
  // only row that offers "Remove from team" at the root — the deprovisioning case.
  // (`viewer` is self → "Leave this team"; `marcus` is inherited → "Open Platform".)
  root: [
    memberRow("viewer", "root", "Tideline", "admin"),
    memberRow("dana", "root", "Tideline", "member"),
    memberRow("marcus", "platform", "Platform", "member"),
  ],
  platform: [memberRow("marcus", "platform", "Platform", "member")],
};

const DASH = { user: VIEWER, org: TEAMS[0], teams: TEAMS, pending_invitations: [], is_org_admin: false };
const CREDITS = { tier_key: "pro", tier_name: "Pro", pct_used: 10, reset_at: null, prepaid_credits: 0 };

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// When false, the viewer is a plain member: every admin-gated endpoint (rosters, team invitations)
// answers 403, which the seam resolves into the `forbidden` detail.
let viewerManages = true;

/** What `/auth/me` says the viewer administers (descent included) — the org admin by default. */
let adminTeamIds: string[] = ["root", "platform"];

/** Teams beyond the base two, for the cases that need a wider org. */
let extraTeams: unknown[] = [];

/** What `/api/v1/invitations/me` answers — the viewer's OWN pending invitations. */
let myInvitations: unknown[] = [];

function route(req: Request): Response {
  const p = new URL(req.url).pathname;
  if (p === "/api/v1/auth/me") return json({ ...VIEWER, admin_team_ids: adminTeamIds });
  if (p === "/api/v1/teams") return json([...TEAMS, ...extraTeams]);
  if (p === "/api/v1/dashboard") return json(DASH);
  if (p === "/api/v1/me/credits") return json(CREDITS);
  if (p === "/api/v1/invitations/me") return json(myInvitations);
  const members = p.match(/^\/api\/v1\/teams\/([^/]+)\/members$/);
  if (members) {
    if (!viewerManages || !adminTeamIds.includes(members[1])) return json({ detail: "team admin role required" }, 403);
    return json(ROSTERS[members[1]] ?? []);
  }
  if (/^\/api\/v1\/teams\/[^/]+\/invitations$/.test(p)) {
    if (!viewerManages) return json({ detail: "You don't administer this team." }, 403);
    return json([]);
  }
  return json({ detail: `unmatched ${p}` }, 404);
}

let fetchSpy: ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
beforeEach(() => {
  queryClient.clear();
  resetTeamsStore();
  viewerManages = true;
  adminTeamIds = ["root", "platform"];
  extraTeams = [];
  myInvitations = [];
  fetchSpy = vi.fn(async (input: Request | string, init?: RequestInit) =>
    route(input instanceof Request ? input : new Request(input, init)),
  );
  vi.stubGlobal("fetch", fetchSpy);
});

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <AppContent />
    </MemoryRouter>,
  );

/** The roster fetches that hit a team's members endpoint. */
const rosterRequests = (teamId: string): Request[] =>
  fetchSpy.mock.calls
    .map((c) => c[0] as Request)
    .filter((r) => new URL(r.url).pathname === `/api/v1/teams/${teamId}/members`);

const invitesPanel = () => screen.queryByRole("dialog", { name: /your invitations/i });

/** The membership DELETEs actually sent, in order. */
const removalRequests = (): Request[] =>
  fetchSpy.mock.calls
    .map((c) => c[0] as Request)
    .filter((r) => r.method === "DELETE" && /\/memberships\//.test(new URL(r.url).pathname));

/** Open a roster row's action menu and click its destructive "Remove from team". */
const clickRemove = async (user: ReturnType<typeof userEvent.setup>, name: string) => {
  const row = (await screen.findByText(name, {}, { timeout: 5000 })).closest("tr")!;
  await user.click(within(row).getByRole("button", { name: /actions|more/i }));
  await user.click(await screen.findByRole("menuitem", { name: "Remove from team" }));
};

// Removing someone at the ORG ROOT is deprovisioning, not a team edit: the backend
// deactivates the account and revokes every session and CLI token. The asymmetry is
// the whole point — a sub-team removal must stay one click, and the root must not
// fire until the admin has been told what it does.
describe("removing a member: sub-team edit vs org deprovisioning", () => {
  it("a SUB-TEAM removal is confirmed first, and sends nothing until it is", async () => {
    const user = userEvent.setup();
    renderAt("/teams/platform");
    await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 });

    await clickRemove(user, "Marcus");

    const dialog = await screen.findByRole("dialog", { name: /remove marcus from platform/i });
    expect(removalRequests()).toHaveLength(0);
    // Not the deprovisioning prompt: nothing about the account.
    expect(within(dialog).queryByText(/deactivat/i)).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));

    await waitFor(() => expect(removalRequests()).toHaveLength(1));
    expect(new URL(removalRequests()[0].url).pathname).toBe("/api/v1/teams/platform/memberships/marcus");
  });

  it("a removal that cascades says which teams it takes the person out of", async () => {
    // root → platform → runtime: removing someone from Platform also removes them from Runtime
    // (below) — and Platform's parent is the root, which a removal never touches.
    extraTeams = [
      { id: "runtime", name: "Runtime", parent_team_id: "platform", is_root: false, created_at: "2026-01-01T00:00:00Z", member_count: 0 },
    ];
    const user = userEvent.setup();
    renderAt("/teams/platform");
    await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 });

    await clickRemove(user, "Marcus");
    const dialog = await screen.findByRole("dialog", { name: /remove marcus from platform/i });
    expect(within(dialog).getByText("They also leave every team under Platform.")).toBeInTheDocument();
  });

  it("the org owner is not offered to remove themself from the org root", async () => {
    // The server refuses it ("You cannot remove yourself from the organization"), and the dialog it
    // opened described deactivating the reader's own account in the third person.
    const user = userEvent.setup();
    renderAt("/teams/root");
    const row = (await screen.findByText("Viewer", {}, { timeout: 5000 })).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: /actions|more/i }));
    expect(await screen.findByRole("menuitem", { name: "View member" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /leave this team/i })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /remove from team/i })).toBeNull();
  });

  it("at the org root nothing is sent until the confirm", async () => {
    const user = userEvent.setup();
    renderAt("/teams/root");
    await screen.findByRole("heading", { name: "Tideline" }, { timeout: 5000 });

    await clickRemove(user, "Dana");

    // The click alone must not deprovision anyone.
    const dialog = await screen.findByRole("dialog", { name: /remove dana from the organization/i });
    expect(removalRequests()).toHaveLength(0);
    // The copy has to say what actually happens, not "removed from a team".
    expect(within(dialog).getByText(/deactivated/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/signed out/i)).toBeInTheDocument();

    // Backing out sends nothing either.
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(removalRequests()).toHaveLength(0);

    cleanup();
    renderAt("/teams/root");
    await screen.findByRole("heading", { name: "Tideline" }, { timeout: 5000 });
    await clickRemove(user, "Dana");
    const again = await screen.findByRole("dialog", { name: /remove dana from the organization/i });
    await user.click(within(again).getByRole("button", { name: "Deactivate account" }));
    await waitFor(() => expect(removalRequests()).toHaveLength(1));
    expect(new URL(removalRequests()[0].url).pathname).toBe("/api/v1/teams/root/memberships/dana");
  });

  it("the deprovision prompt opens on Cancel, where Enter does nothing at all", async () => {
    // Deactivating an account is one of the loudest things this page does; the shared
    // confirmation opens with Cancel focused and swallows Enter entirely, so a reflex
    // press neither deactivates the account nor loses the question.
    const user = userEvent.setup();
    renderAt("/teams/root");
    await screen.findByRole("heading", { name: "Tideline" }, { timeout: 5000 });

    await clickRemove(user, "Dana");
    await screen.findByRole("dialog", { name: /remove dana from the organization/i });

    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus());
    await user.keyboard("{Enter}");

    expect(screen.getByRole("dialog", { name: /remove dana from the organization/i })).toBeInTheDocument();
    expect(removalRequests()).toHaveLength(0);
  });
});

describe("Teams deep links", () => {
  it("the team in the path seeds the selection, the root without one", async () => {
    renderAt("/teams");
    expect(await screen.findByRole("heading", { name: "Tideline" }, { timeout: 5000 })).toBeInTheDocument();
    expect(invitesPanel()).not.toBeInTheDocument();

    cleanup();
    renderAt("/teams/platform");
    expect(await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 })).toBeInTheDocument();
    // The seam fetched the deep-linked team's roster, not just the root's.
    await waitFor(() => expect(rosterRequests("platform").length).toBeGreaterThan(0));
  });

  it("picking a team in the team tree navigates to it", async () => {
    const user = userEvent.setup();
    renderAt("/teams");
    await screen.findByRole("heading", { name: "Tideline" }, { timeout: 5000 });
    // The org actions appear once the roster proves the viewer manages the team.
    await user.click(await screen.findByRole("button", { name: "Team tree" }));
    const tree = await screen.findByRole("dialog", { name: /team tree/i });
    await user.click(within(tree).getByText("Platform"));
    expect(await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 })).toBeInTheDocument();
  });

  it("?tab=invites opens the panel whether or not the key is on screen", async () => {
    // The link is in the invitation email, so it has to work for a reader with
    // nothing pending — they may have accepted it in another tab a moment ago.
    const user = userEvent.setup();
    renderAt("/teams?tab=invites");
    const panel = await screen.findByRole("dialog", { name: /your invitations/i }, { timeout: 5000 });
    await user.click(within(panel).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(invitesPanel()).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /invitations/i })).not.toBeInTheDocument();
  });

  it("offers the key only while the reader has an invitation, and says whose it is", async () => {
    // The ORG's outgoing invitations live beside the people they are for, on the
    // team. This key is the reader's own, so it says so — and a doorway to an
    // empty list is a doorway nobody should be shown.
    const user = userEvent.setup();
    renderAt("/teams");
    await screen.findByRole("button", { name: "Team tree" }, { timeout: 5000 });
    expect(screen.queryByRole("button", { name: /invitations/i })).not.toBeInTheDocument();
    cleanup();

    myInvitations = [
      {
        id: "inv_1",
        email: "vera@x.io",
        team_id: "platform",
        team_name: "Platform",
        org_name: "Tideline",
        role: "member",
        role_display: "Member",
        expires_at: "2026-12-01T00:00:00Z",
        refusal: null,
      },
    ];
    queryClient.clear();
    renderAt("/teams");
    const key = await screen.findByRole("button", { name: /your invitations/i }, { timeout: 5000 });
    expect(key).toHaveTextContent("1");
    await user.click(key);
    expect(await screen.findByRole("dialog", { name: /your invitations/i })).toBeInTheDocument();
  });

  // The two entries live in mailed links, so a dropped redirect strands a real user.
  it("the legacy /dashboard entries redirect with their target intact", async () => {
    renderAt("/dashboard/teams/platform");
    expect(await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 })).toBeInTheDocument();

    cleanup();
    renderAt("/dashboard/invites");
    expect(await screen.findByRole("dialog", { name: /your invitations/i }, { timeout: 5000 })).toBeInTheDocument();
  });
});

describe("Teams as a sub-team admin", () => {
  // The viewer administers Platform (an admin row there) and nothing above it: the root's roster,
  // and creating, moving and deleting teams, are refused to them by the server.
  beforeEach(() => {
    adminTeamIds = ["platform"];
  });

  it("/teams opens on the team they lead, without asking for the root's roster", async () => {
    renderAt("/teams");
    expect(await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText(/don’t manage this team/i)).toBeNull();
    expect(rosterRequests("root")).toHaveLength(0);
  });

  it("is offered only what the server allows a sub-team admin", async () => {
    const user = userEvent.setup();
    renderAt("/teams/platform");
    await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 });
    await screen.findByText("Marcus", {}, { timeout: 5000 });

    await user.click(screen.getByRole("button", { name: "Manage Platform" }));
    expect(await screen.findByRole("menuitem", { name: "Rename team" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /move team/i })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /delete team/i })).toBeNull();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("button", { name: /create sub-team/i })).toBeNull();
    expect(screen.getByText("Platform has no sub-teams.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Team tree" }));
    const tree = await screen.findByRole("dialog", { name: /team tree/i });
    expect(within(tree).queryByRole("button", { name: "Create team" })).toBeNull();
  });

  it("adds people by address alone, without claiming who is in the organization", async () => {
    // Only an org admin can read the org directory (the root's roster); a sub-team admin's dialog
    // must not ask for it, nor tell them an address "isn't in your organization" — it may be, and
    // the server then adds that person at once.
    const user = userEvent.setup();
    renderAt("/teams/platform");
    await screen.findByText("Marcus", {}, { timeout: 5000 });
    await user.click(screen.getByRole("button", { name: "Add member" }));
    const search = await screen.findByRole("searchbox", { name: /search people or type an email address/i });
    await user.type(search, "new.person@x.io");

    const dialog = screen.getByRole("dialog", { name: /add member to platform/i });
    expect(await within(dialog).findByText(/already in your organization joins Platform at once/i)).toBeInTheDocument();
    expect(within(dialog).queryByText(/isn’t in your organization/i)).toBeNull();
    expect(rosterRequests("root")).toHaveLength(0);
  });

  it("the tree shows the org but opens only the teams descent grants", async () => {
    const user = userEvent.setup();
    renderAt("/teams/platform");
    await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 });
    await user.click(await screen.findByRole("button", { name: "Team tree" }));
    const tree = await screen.findByRole("dialog", { name: /team tree/i });

    const root = within(tree).getByRole("treeitem", { name: /^Tideline/ });
    expect(root).toHaveAttribute("aria-disabled", "true");
    await user.click(within(root).getByText("Tideline"));
    // Still on Platform: the root is not theirs to open.
    expect(screen.getByRole("heading", { name: "Platform" })).toBeInTheDocument();
    expect(within(tree).getByRole("treeitem", { name: /^Platform/ })).not.toHaveAttribute("aria-disabled", "true");
  });

  it("a link to a team above them offers the way back to theirs", async () => {
    const user = userEvent.setup();
    renderAt("/teams/root");
    expect(await screen.findByText(/don’t manage this team/i, undefined, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText(/pick a team you administer/i)).toBeNull();
    expect(rosterRequests("root")).toHaveLength(0);
    // The tree stays reachable from here.
    expect(screen.getByRole("button", { name: "Team tree" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Go to Platform" }));
    expect(await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 })).toBeInTheDocument();
  });

  it("someone leading two unrelated teams chooses between them", async () => {
    extraTeams = [
      { id: "data", name: "Data", parent_team_id: "root", is_root: false, created_at: "2026-01-01T00:00:00Z", member_count: 0 },
    ];
    adminTeamIds = ["platform", "data"];
    const user = userEvent.setup();
    renderAt("/teams");
    const picker = await screen.findByRole("region", { name: "Teams you administer" }, { timeout: 5000 });
    expect(within(picker).getByRole("button", { name: "Data" })).toBeInTheDocument();
    expect(rosterRequests("root")).toHaveLength(0);
    await user.click(within(picker).getByRole("button", { name: "Platform" }));
    expect(await screen.findByRole("heading", { name: "Platform" }, { timeout: 5000 })).toBeInTheDocument();
  });
});

describe("Teams as a plain member", () => {
  beforeEach(() => {
    adminTeamIds = [];
  });

  it("an invitation reaches a plain member too", async () => {
    // The invitations entry must not depend on the org actions, which a member never gets.
    viewerManages = false;
    myInvitations = [
      {
        id: "inv_2",
        email: "vera@x.io",
        team_id: "elsewhere",
        team_name: "Research",
        org_name: "Other Co",
        role: "member",
        role_display: "Member",
        expires_at: "2026-12-01T00:00:00Z",
        refusal: { code: "other_org", message: "Your account belongs to Tideline, and an account can belong to only one organization." },
      },
    ];
    renderAt("/teams");
    expect(await screen.findByRole("button", { name: /your invitations/i }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Team tree" })).toBeNull();
  });

  // A member (every roster fetch 403s) reaches ONLY the "you don't manage this team" surface: the
  // topbar's org actions, the breadcrumb's team names, and the connections plate all stay hidden.
  it("a member sees the forbidden surface and nothing else", async () => {
    viewerManages = false;
    renderAt("/teams/platform");
    expect(await screen.findByText(/don’t manage this team/i, undefined, { timeout: 5000 })).toBeInTheDocument();
    // The header still names the selected team.
    expect(screen.getByRole("heading", { name: "Platform" })).toBeInTheDocument();
    // The org-surveying topbar actions are withheld.
    expect(screen.queryByRole("button", { name: "Team tree" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /invitations/i })).not.toBeInTheDocument();
    // So are the breadcrumb (it names other teams) and the connections plate.
    expect(screen.queryByRole("navigation", { name: /team ancestry/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/preconfigured connections/i)).not.toBeInTheDocument();
  });
});

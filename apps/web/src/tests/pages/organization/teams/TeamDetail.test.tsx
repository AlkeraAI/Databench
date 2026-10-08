import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TABLE_PAGER_LABELS } from "@alkera/ui";
import type { components } from "@alkera/sdk";

import { toTeamsGraph, type TeamsWire } from "@/pages/organization/teams/data/adapt";
import { TeamDetail, type DetailActions } from "@/pages/organization/teams/detail/TeamDetail";
import { memberCountLabel, type DetailStatus } from "@/pages/organization/teams/data/model";

// TeamDetail resolves the selected team's `detail.status` into a first-class surface. These assert
// the surface a user actually reaches in each state — never class names — and the rule that manage
// actions appear only when the viewer can see the roster (i.e. is an admin here).

type MemberWire = components["schemas"]["TeamMemberRead"];

// The ready-state detail embeds the Preconfigured Connections plate (React
// Query + fetch of the team's connections); give it a client and a quiet empty
// response so these state tests stay focused on the roster surfaces.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

function withProviders(node: React.ReactElement, entry = "/teams") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>{node}</MemoryRouter>
    </QueryClientProvider>
  );
}

/** One wire row for `user` on the team: `direct` is the row written there (null = none), `from` the
 *  team above whose admin row reaches it (null = nothing does); `role` follows, admin when either
 *  side is admin — exactly as the backend decides it. */
const m = (
  user: string,
  teamId: string,
  teamName: string,
  direct: "admin" | "member" | null,
  from: { id: string; name: string } | null = null,
): MemberWire => {
  const role = direct === "admin" || from ? "admin" : "member";
  return {
    user_id: user,
    display_name: user[0].toUpperCase() + user.slice(1),
    email: `${user}@x.io`,
    first_name: user,
    last_name: "X",
    role,
    effective_role: role,
    team_id: teamId,
    team_name: teamName,
    created_at: "2026-02-01T00:00:00Z",
    role_display: role === "admin" ? "Admin" : "Member",
    direct_role: direct,
    descent_role: from ? "admin" : null,
    descent_from_team_id: from?.id ?? null,
    descent_from_team_name: from?.name ?? null,
  };
};

const ORG = { id: "root", name: "Org" };

// root → platform → runtime; the viewer is the org admin: an admin of Platform by descent from the
// root, holding a member row on Platform as well — so in BOTH of Platform's listings.
const wire: TeamsWire = {
  teams: [
    { id: "root", name: "Org", parent_team_id: null, is_root: true, created_at: "2026-01-01T00:00:00Z", member_count: 3 },
    { id: "platform", name: "Platform", parent_team_id: "root", is_root: false, created_at: "2026-01-01T00:00:00Z", member_count: 2 },
    { id: "runtime", name: "Runtime", parent_team_id: "platform", is_root: false, created_at: "2026-01-01T00:00:00Z", member_count: 1 },
  ],
  viewerId: "viewer",
  adminTeamIds: ["root", "platform", "runtime"],
  selectedId: "platform",
  members: [m("marcus", "platform", "Platform", "admin"), m("viewer", "platform", "Platform", "member", ORG), m("sam", "platform", "Platform", null, ORG)],
  invites: [],
};
const graph = toTeamsGraph(wire);

const noopActions = (): DetailActions => ({
  selectTeam: vi.fn(),
  changeRole: vi.fn(),
  removeMember: vi.fn(),
  moveMember: vi.fn(),
  addMember: vi.fn(),
  createSub: vi.fn(),
  renameTeam: vi.fn(),
  moveTeam: vi.fn(),
  deleteTeam: vi.fn(),
  revokeInvite: vi.fn(),
  viewPlan: vi.fn(),
});

function renderDetail(status: DetailStatus, onRetry = vi.fn(), errorMessage: string | null = null) {
  const actions = noopActions();
  render(
    withProviders(
      <TeamDetail graph={graph} teamId="platform" detail={{ status, errorMessage }} actions={actions} onRetry={onRetry} />,
    ),
  );
  return { actions, onRetry };
}

describe("TeamDetail sections", () => {
  const renderAt = (entry: string) =>
    render(
      withProviders(
        <TeamDetail graph={graph} teamId="platform" detail={{ status: "ready", errorMessage: null }} actions={noopActions()} onRetry={vi.fn()} />,
        entry,
      ),
    );

  // With no extension installed the roster is the whole page: no section strip, whatever the link.
  it.each(["/teams", "/teams?tab=invites", "/teams?tab=allocations"])("opens on the roster with no section tabs at %s", (entry) => {
    renderAt(entry);
    expect(screen.queryByRole("tab", { name: "Members" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Allocations" })).toBeNull();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});

describe("TeamDetail detail states", () => {
  it("ready: the two listings overlap — the org admin is in both, Sam only by descent", async () => {
    const user = userEvent.setup();
    renderDetail("ready");
    // Default scope is Direct members: Marcus and the viewer hold rows; Sam holds none.
    let roster = within(screen.getByRole("table"));
    expect(roster.getByText("Marcus")).toBeInTheDocument();
    expect(roster.getByText("Viewer")).toBeInTheDocument();
    expect(roster.queryByText("Sam")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add member/i })).toBeInTheDocument();

    // Switch to By descent → Sam appears, from the root; the viewer is listed here TOO (both listings).
    await user.click(screen.getByRole("tab", { name: /by descent/i }));
    roster = within(screen.getByRole("table"));
    expect(roster.getByText("Sam")).toBeInTheDocument();
    expect(roster.getByText("Viewer")).toBeInTheDocument();
    expect(roster.queryByText("Marcus")).not.toBeInTheDocument();
    expect(roster.getAllByRole("button", { name: "Org" }).length).toBeGreaterThan(0); // the "from" link
  });

  it("ready: descent locks the role control and states the fact on the row", () => {
    renderDetail("ready");
    const roster = within(screen.getByRole("table"));
    // The viewer's row on Platform says member, but descent from the root makes them admin here:
    // the control is disabled, named with the fact, and no role menu opens for them.
    const locked = roster.getByRole("button", { name: "Role for Viewer: Admin, by descent from Org" });
    expect(locked).toBeDisabled();
    expect(roster.queryByRole("button", { name: /^Role for Viewer: (Member|Admin)$/ })).toBeNull();
    // Marcus's row is decided here, so his control stays live.
    expect(roster.getByRole("button", { name: "Role for Marcus: Admin" })).toBeEnabled();
  });

  it("ready: a row's destructive action removes that member", async () => {
    // End-to-end through the migrated stack: base Table → ActionsMenu registry → base DropdownItem's
    // danger row. Opening Marcus's overflow and picking the destructive command must fire
    // removeMember for THIS member (not moveMember, not another row) and close the menu.
    const user = userEvent.setup();
    const { actions } = renderDetail("ready");
    // The row overflow trigger is named per-member (person + team), so we act on Marcus's, not Sam's.
    await user.click(screen.getByRole("button", { name: /actions for marcus in platform/i }));
    // Non-self direct member → the destructive row is "Remove from team".
    await user.click(screen.getByRole("menuitem", { name: /remove from team/i }));
    expect(actions.removeMember).toHaveBeenCalledOnce();
    expect(actions.moveMember).not.toHaveBeenCalled();
    // The menu dismissed after the live command.
    expect(screen.queryByRole("menuitem", { name: /remove from team/i })).toBeNull();
  });

  it("forbidden: the roster and its actions are absent, not hidden", () => {
    renderDetail("forbidden");
    expect(screen.getByText(/don’t manage this team/i)).toBeInTheDocument();
    // The header still names the team…
    expect(screen.getByRole("heading", { name: "Platform" })).toBeInTheDocument();
    // …but the roster + its manage actions must not render for a non-admin…
    expect(screen.queryByRole("button", { name: /add member/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /manage platform/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    // …nor the breadcrumb (it names the org's other teams) or the connections plate.
    expect(screen.queryByRole("navigation", { name: /team ancestry/i })).not.toBeInTheDocument();
    expect(screen.queryByText("Org")).not.toBeInTheDocument();
    expect(screen.queryByText(/preconfigured connections/i)).not.toBeInTheDocument();
  });

  it("error: surfaces the message and a retry that fires onRetry", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    renderDetail("error", onRetry, "boom (503)");
    expect(screen.getByText(/couldn’t load this team/i)).toBeInTheDocument();
    expect(screen.getByText(/boom \(503\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /try again/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("loading: the frame is already there, the roster is not", () => {
    renderDetail("loading");
    expect(screen.getByLabelText(/loading roster/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    // The page is never bare while the detail resolves.
    expect(screen.getByRole("heading", { name: "Platform" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: /team ancestry/i })).toBeInTheDocument();
  });
});

// The roster ledger's footer states the member count, and the ledger paginates its DIRECT members at
// PAGE_SIZE per page. These drive the ready surface through a LARGER fixture than the base cases above
// (which have too few members to page). The footer label + pager control names are DERIVED from the
// component's own exported formatter (`memberCountLabel`) and the Table's exported `TABLE_PAGER_LABELS`
// — never a copied literal — so a format change moves source and test together.
const PAGE_SIZE = 10;

/** N direct members of Platform: distinct users, each holding a member row on Platform, plus the
 *  viewer reaching it by descent alone (so the direct listing is exactly the N). */
function platformRosterWire(memberCount: number): TeamsWire {
  const members: MemberWire[] = [];
  for (let i = 0; i < memberCount; i++) {
    members.push(m(`person${i}`, "platform", "Platform", "member"));
  }
  members.push(m("viewer", "platform", "Platform", null, ORG));
  return {
    teams: [
      { id: "root", name: "Org", parent_team_id: null, is_root: true, created_at: "2026-01-01T00:00:00Z", member_count: memberCount + 1 },
      { id: "platform", name: "Platform", parent_team_id: "root", is_root: false, created_at: "2026-01-01T00:00:00Z", member_count: memberCount },
    ],
    viewerId: "viewer",
    adminTeamIds: ["root", "platform"],
    selectedId: "platform",
    members,
    invites: [],
  };
}

function renderRoster(memberCount: number) {
  const largeGraph = toTeamsGraph(platformRosterWire(memberCount));
  render(
    withProviders(
      <TeamDetail graph={largeGraph} teamId="platform" detail={{ status: "ready", errorMessage: null }} actions={noopActions()} onRetry={vi.fn()} />,
    ),
  );
}

/** Data cells in the roster's Member column — one per rendered member row (excludes the header row,
 *  which has no name cell). */
function memberNameCells(): HTMLElement[] {
  const table = screen.getByRole("table");
  return within(table)
    .getAllByRole("cell")
    .filter((c) => c.querySelector(".alk-identity__name"));
}

describe("Teams roster footer + pagination", () => {
  // The pager earns its place only past one page; the footer counts the whole roster either way. The
  // label comes from the component's own `memberCountLabel`, so wording can't drift from the source.
  it.each([6, PAGE_SIZE])("counts %i members with no pager", (count) => {
    renderRoster(count);
    expect(screen.getByText(memberCountLabel(count))).toBeInTheDocument();
    expect(memberNameCells()).toHaveLength(count);
    expect(screen.queryByRole("button", { name: TABLE_PAGER_LABELS.next })).toBeNull();
  });

  // Past the page size → a pager appears AND only a page's worth of rows render at once. Catches a
  // wrong impl that renders every row (no windowing) or hides the pager past one page.
  it("pages past the page size, rendering one window at a time", async () => {
    const user = userEvent.setup();
    const total = PAGE_SIZE + 3; // 13 → two pages (10 + 3)
    renderRoster(total);

    // The footer still counts the WHOLE roster, not just the visible page.
    expect(screen.getByText(memberCountLabel(total))).toBeInTheDocument();
    // Only a page's worth of member rows are in the DOM at once.
    expect(memberNameCells()).toHaveLength(PAGE_SIZE);
    // The pager earns its place — prev disabled on page 1, next enabled.
    const next = screen.getByRole("button", { name: TABLE_PAGER_LABELS.next });
    expect(screen.getByRole("button", { name: TABLE_PAGER_LABELS.previous })).toBeDisabled();
    expect(next).toBeEnabled();

    // Advancing shows the remainder — the last page holds total - PAGE_SIZE rows.
    await user.click(next);
    expect(memberNameCells()).toHaveLength(total - PAGE_SIZE);
    // On the last page, next is disabled and prev is enabled — the window actually moved.
    expect(screen.getByRole("button", { name: TABLE_PAGER_LABELS.next })).toBeDisabled();
    expect(screen.getByRole("button", { name: TABLE_PAGER_LABELS.previous })).toBeEnabled();
  });
});

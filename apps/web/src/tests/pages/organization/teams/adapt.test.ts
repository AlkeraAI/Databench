import { describe, expect, it } from "vitest";
import type { components } from "@alkera/sdk";

import { toDirectory, toTeamsGraph, type TeamsWire } from "@/pages/organization/teams/data/adapt";
import {
  descentAdmins,
  descentCount,
  descentRoster,
  directAdmins,
  directCount,
  directRoster,
  teamMemberCount,
  viewerDescentSource,
} from "@/pages/organization/teams/data/model";

// The wire states, per row, how a person stands on the selected team: the row written on it
// (`direct_role`), the admin reaching it from above (`descent_role` + `descent_from_team_*`), and
// the standing the two make (`role`). The adapter maps that into two listings that may OVERLAP —
// it must never re-derive direct-ness from depth, row order, or any other proxy. That inference is
// the regression these guard: the org admin added to a sub-team vanished from the root's direct
// listing and reappeared below as a member. Every assertion reads the view-model helpers —
// observable output, never the adapter's internals.

type TeamWire = components["schemas"]["TeamRead"];
type MemberWire = components["schemas"]["TeamMemberRead"];

const NAMES: Record<string, [string, string]> = {
  viewer: ["Vee", "Root"],
  priya: ["Priya", "Anand"],
  marcus: ["Marcus", "Bell"],
  dana: ["Dana", "Whitfield"],
};
const T = {
  root: { id: "t-root", name: "Tideline", parent: null, root: true },
  eng: { id: "t-eng", name: "Engineering", parent: "t-root" },
  test: { id: "t-test", name: "Test", parent: "t-eng" },
} as const;

function teamWire(t: { id: string; name: string; parent: string | null; root?: boolean }, memberCount: number): TeamWire {
  return { id: t.id, name: t.name, parent_team_id: t.parent, is_root: Boolean(t.root), created_at: "2026-01-01T00:00:00Z", member_count: memberCount };
}

interface Standing {
  direct?: "admin" | "member" | null;
  from?: { id: string; name: string } | null;
}

/** One wire row for `user` on `team`, as the backend decides it: `direct` is the row on the team
 *  (null = none), `from` the nearest team above whose admin row reaches it (null = nothing does),
 *  and `role` follows — admin when either side is admin. */
function row(user: string, team: { id: string; name: string }, { direct = "member", from = null }: Standing = {}): MemberWire {
  const [first, last] = NAMES[user];
  const role = direct === "admin" || from ? "admin" : "member";
  return {
    user_id: user,
    display_name: `${first} ${last}`,
    email: `${user}@x.io`,
    first_name: first,
    last_name: last,
    role,
    effective_role: role,
    team_id: team.id,
    team_name: team.name,
    created_at: "2026-03-01T00:00:00Z",
    role_display: role === "admin" ? "Admin" : "Member",
    direct_role: direct,
    descent_role: from ? "admin" : null,
    descent_from_team_id: from?.id ?? null,
    descent_from_team_name: from?.name ?? null,
  };
}

const ALL_TEAMS: TeamWire[] = [teamWire(T.root, 4), teamWire(T.eng, 2), teamWire(T.test, 1)];

/** Test, selected. Marcus holds a member row there; Priya, Engineering's admin, reaches it by descent
 *  with no row; the viewer — the org admin — holds a member row on Test AND reaches it from the root. */
function testWire(overrides: Partial<TeamsWire> = {}): TeamsWire {
  return {
    teams: ALL_TEAMS,
    viewerId: "viewer",
    adminTeamIds: ALL_TEAMS.map((t) => t.id),
    selectedId: T.test.id,
    members: [
      row("marcus", T.test),
      row("priya", T.test, { direct: null, from: T.eng }),
      row("viewer", T.test, { direct: "member", from: T.root }),
    ],
    invites: [],
    ...overrides,
  };
}

describe("toTeamsGraph — the two listings overlap", () => {
  const g = toTeamsGraph(testWire());

  it("lists everyone holding a row on the team as a direct member, the org admin included", () => {
    const ids = directRoster(g, T.test.id).map((e) => e.person.id).sort();
    expect(ids).toEqual(["marcus", "viewer"]);
    expect(directCount(g, T.test.id)).toBe(2);
  });

  it("lists everyone an admin row above reaches as a member by descent, from the team named by the wire", () => {
    const byId = Object.fromEntries(descentRoster(g, T.test.id).map((e) => [e.person.id, e]));
    expect(Object.keys(byId).sort()).toEqual(["priya", "viewer"]);
    expect(byId.priya.descentFrom).toEqual({ id: T.eng.id, name: T.eng.name });
    expect(byId.viewer.descentFrom).toEqual({ id: T.root.id, name: T.root.name });
    expect(descentCount(g, T.test.id)).toBe(2);
  });

  it("keeps one person in BOTH listings when they hold a row and are reached from above", () => {
    const direct = directRoster(g, T.test.id).find((e) => e.person.id === "viewer");
    const descent = descentRoster(g, T.test.id).find((e) => e.person.id === "viewer");
    expect(direct?.listing).toBe("direct");
    expect(descent?.listing).toBe("descent");
    // The row says member; the standing is admin because descent wins — on both entries.
    expect(direct?.directRole).toBe("member");
    expect(direct?.role).toBe("admin");
    expect(descent?.role).toBe("admin");
  });

  it("a person reached by descent alone has no direct role and is not a direct member", () => {
    const priya = descentRoster(g, T.test.id).find((e) => e.person.id === "priya");
    expect(priya?.directRole).toBeNull();
    expect(directRoster(g, T.test.id).map((e) => e.person.id)).not.toContain("priya");
  });

  it("a member row reached by nothing from above is a plain member in one listing only", () => {
    const marcus = directRoster(g, T.test.id).find((e) => e.person.id === "marcus");
    expect(marcus?.role).toBe("member");
    expect(marcus?.descentFrom).toBeNull();
    expect(descentRoster(g, T.test.id).map((e) => e.person.id)).not.toContain("marcus");
  });

  it("governance reads the by-descent listing and the direct admin rows, never a proxy", () => {
    expect(descentAdmins(g, T.test.id).map((a) => [a.person.id, a.fromTeamName])).toEqual([
      ["priya", T.eng.name],
      ["viewer", T.root.name],
    ]);
    // The viewer's member row on Test is not an admin row; only an admin row here counts as direct.
    expect(directAdmins(g, T.test.id)).toEqual([]);
    expect(viewerDescentSource(g, T.test.id)?.id).toBe(T.root.id);
  });

  it("the viewer holding an admin row on the team is not shown as governing it by descent", () => {
    const g2 = toTeamsGraph(testWire({ members: [row("viewer", T.test, { direct: "admin", from: T.root })] }));
    expect(viewerDescentSource(g2, T.test.id)).toBeNull();
    expect(directAdmins(g2, T.test.id).map((p) => p.id)).toEqual(["viewer"]);
  });
});

describe("toTeamsGraph — the root is unaffected by anything below it", () => {
  it("the org admin stays a direct admin of the root, with nothing reaching the root from above", () => {
    // The root's own roster: the wire says the viewer's row there is admin and nothing descends
    // onto a root. Whatever rows exist on Test, this listing must not move.
    const g = toTeamsGraph(
      testWire({
        selectedId: T.root.id,
        members: [row("viewer", T.root, { direct: "admin" }), row("marcus", T.root), row("dana", T.root)],
      }),
    );
    const viewer = directRoster(g, T.root.id).find((e) => e.person.id === "viewer");
    expect(viewer?.directRole).toBe("admin");
    expect(viewer?.role).toBe("admin");
    expect(descentRoster(g, T.root.id)).toEqual([]);
    expect(directCount(g, T.root.id)).toBe(3);
  });

  it("a team's badge count is the wire's row count for that team, for every team in the org", () => {
    const g = toTeamsGraph(testWire());
    expect(teamMemberCount(g, T.root.id)).toBe(4);
    expect(teamMemberCount(g, T.eng.id)).toBe(2);
    expect(teamMemberCount(g, T.test.id)).toBe(1);
    expect(teamMemberCount(g, "nope")).toBe(0);
  });
});

describe("toDirectory", () => {
  it("is the root's direct rows, name-sorted, and never a by-descent entry", () => {
    const people = toDirectory([
      row("marcus", T.root),
      row("dana", T.root, { direct: "admin" }),
      row("priya", T.root, { direct: null, from: T.eng }),
    ]);
    expect(people.map((p) => p.id)).toEqual(["dana", "marcus"]);
    expect(people[0].initials).toBe("DW");
  });
});

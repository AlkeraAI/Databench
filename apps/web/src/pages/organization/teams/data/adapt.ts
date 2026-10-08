// Wire → view-model. Pure (data in, data out) so it is unit-testable with no network —
// the same adapter the live provider and any fixture run through.
//
// `GET /teams/{id}/members` returns one row per person who stands on the team, and the
// wire has already decided how: `direct_role` is the row written on the team (null when
// the person reaches it by descent alone), `descent_role` + `descent_from_team_*` is the
// admin reaching it from the nearest team above holding the admin row, and `role` is the
// standing the two make. The adapter maps rows; it never infers direct-ness from depth
// or any other proxy — that inference is exactly what made an org admin added to a
// sub-team vanish from the root's direct listing.

import type { components } from "@alkera/sdk";

import { formatDate } from "@/lib/format/date";
import { type Graph, type Person, type Role, type Standing, type Team } from "./model";

type TeamWire = components["schemas"]["TeamRead"];
type MemberWire = components["schemas"]["TeamMemberRead"];
type InvitationWire = components["schemas"]["InvitationRead"];

/** Everything the adapter needs for one selected team. The provider fetches it;
 *  a fixture builds it by hand. */
export interface TeamsWire {
  teams: TeamWire[];
  viewerId: string;
  /** Every team the viewer administers, descent included (`/auth/me` → `admin_team_ids`). */
  adminTeamIds: string[];
  selectedId: string;
  /** The selected team's roster: one row per person who stands on it, either way. */
  members: MemberWire[];
  /** The selected team's pending invitations. */
  invites: InvitationWire[];
}

function initialsFor(first: string, last: string, display: string): string {
  const a = first.trim()[0] ?? "";
  const b = last.trim()[0] ?? "";
  if (a || b) return (a + b).toUpperCase();
  const parts = display.trim().split(/\s+/).filter(Boolean);
  return (parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0]?.slice(0, 2) ?? "?")).toUpperCase();
}

function personFromRow(row: MemberWire): Person {
  return {
    id: row.user_id,
    name: row.display_name,
    email: row.email,
    initials: initialsFor(row.first_name, row.last_name, row.display_name),
  };
}

export function mapTeam(t: TeamWire): Team {
  return { id: t.id, name: t.name, parentId: t.parent_team_id, isRoot: t.is_root, memberCount: t.member_count };
}

const asRole = (r: string): Role => (r === "admin" ? "admin" : "member");

function standingFromRow(row: MemberWire): Standing {
  const from = row.descent_from_team_id && row.descent_from_team_name ? { id: row.descent_from_team_id, name: row.descent_from_team_name } : null;
  return {
    personId: row.user_id,
    directRole: row.direct_role ? asRole(row.direct_role) : null,
    descentFrom: row.descent_role ? from : null,
    role: asRole(row.role),
    joined: formatDate(row.created_at),
  };
}

/** Build the page graph for one selected team. `standing[selectedId]` is the team's
 *  roster as the wire states it; it is deliberately absent for every other team. */
export function toTeamsGraph(wire: TeamsWire): Graph {
  const { teams, viewerId, adminTeamIds, selectedId, members, invites } = wire;
  const teamView = teams.map(mapTeam);
  const root = teamView.find((t) => t.isRoot);

  const people: Record<string, Person> = {};
  const standing: Standing[] = [];
  for (const row of members) {
    // Identity (name/email/initials) is row-invariant for a user, so first-seen wins deterministically.
    people[row.user_id] ??= personFromRow(row);
    standing.push(standingFromRow(row));
  }

  return {
    teams: teamView,
    people,
    standing: { [selectedId]: standing },
    invites: {
      [selectedId]: invites.map((inv) => ({
        id: inv.id,
        email: inv.email,
        role: asRole(inv.role),
        expires: formatDate(inv.expires_at),
      })),
    },
    viewerId,
    orgName: root?.name ?? "your organization",
    rootId: root?.id ?? selectedId,
    adminTeamIds,
  };
}

/** The org user directory — everyone in the org, for the "add member" picker. Sourced
 *  from the root team's roster: every org member holds a row on the root, and nothing
 *  reaches the root by descent, so its direct rows ARE the org. */
export function toDirectory(rootMembers: MemberWire[]): Person[] {
  return rootMembers
    .filter((row) => row.direct_role !== null)
    .map(personFromRow)
    .sort((a, b) => a.name.localeCompare(b.name));
}

// What the viewer may do on the Teams page — the server's policy, read off the roles the API
// returns, so a control is shown only when the request behind it would be allowed. Pure: data in,
// answers out, no fetch.
//
// The one input is `adminTeamIds` (`/auth/me` → `admin_team_ids`): every team the viewer
// administers, descent included — an admin row on a team makes its holder an admin of every team
// beneath it. The server gates each action on the same fact:
//   • a team's roster, its invitations, renaming it, adding / removing / re-roling its members →
//     admin of that team (the route's team-admin dependency, descent applied);
//   • moving a member → admin of BOTH the team they leave and the team they join;
//   • creating, moving and deleting teams → org admin, i.e. admin of the org root;
//   • removing yourself from the org root → never (that is deactivating your own account).
// A stale list fails closed on the page: a team it leaves out renders the "you don't manage this
// team" surface; a team it wrongly includes is still refused by the server, which the page shows
// as the same surface.

import type { Team } from "./model";

/** The slice of the page graph these answers read. */
export interface ViewerScope {
  teams: Team[];
  rootId: string;
  viewerId: string;
  adminTeamIds: readonly string[];
}

/** Whether the viewer administers `teamId` (descent included). */
export function administers(g: Pick<ViewerScope, "adminTeamIds">, teamId: string): boolean {
  return g.adminTeamIds.includes(teamId);
}

/** Org admin = admin of the org root, which by descent is admin everywhere. */
export function isOrgAdmin(g: Pick<ViewerScope, "adminTeamIds" | "rootId">): boolean {
  return administers(g, g.rootId);
}

export interface TeamAbilities {
  /** See the roster and invitations, rename the team, add, invite, re-role and remove members. */
  manage: boolean;
  /** Create a sub-team, move the team, delete it. */
  restructure: boolean;
}

export function abilitiesOn(g: ViewerScope, teamId: string): TeamAbilities {
  return { manage: administers(g, teamId), restructure: isOrgAdmin(g) };
}

/** The teams the viewer administers that no other team they administer sits above — where their
 *  admin rows are. Every other team they administer is beneath one of these. Sorted by name. */
export function administeredTops(g: ViewerScope): Team[] {
  const admin = new Set(g.adminTeamIds);
  return g.teams
    .filter((t) => admin.has(t.id) && !(t.parentId !== null && admin.has(t.parentId)))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** Where `/teams` opens with no team in the URL: the org root for an org admin (and for someone
 *  who administers nothing — they get the read-only surface); the one team a sub-team admin leads;
 *  or a choice when they lead several unrelated teams. */
export type Landing = { kind: "team"; teamId: string } | { kind: "pick"; teams: Team[] };

export function landingFor(g: ViewerScope): Landing {
  if (isOrgAdmin(g)) return { kind: "team", teamId: g.rootId };
  const tops = administeredTops(g);
  if (tops.length === 1) return { kind: "team", teamId: tops[0].id };
  if (tops.length > 1) return { kind: "pick", teams: tops };
  return { kind: "team", teamId: g.rootId };
}

/** Whether a direct row on `teamId` may be removed by the viewer: an admin there may remove anyone
 *  but themself from the org root — that would deactivate their own account, which the server
 *  refuses. */
export function canRemove(g: ViewerScope, teamId: string, personId: string): boolean {
  return administers(g, teamId) && !(teamId === g.rootId && personId === g.viewerId);
}

/** The teams a member of `fromTeamId` can be moved to by the viewer: any other team they
 *  administer (the server requires admin of the destination as well as the source). */
export function moveTargets(g: ViewerScope, fromTeamId: string): Team[] {
  return g.teams.filter((t) => t.id !== fromTeamId && administers(g, t.id));
}

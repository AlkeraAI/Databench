// The Teams view-model — the SEAM between the page's components and the wire.
// Components consume this normalized `Graph` (via the `useTeamsData()` store selector
// in provider.tsx); they never touch the SDK or React Query directly. The page runs the
// live source for its current selection via `useLiveTeamsSync`, which fetches the real
// endpoints, adapts them (adapt.ts), and pushes the resolved state into the store.
//
// SHAPED AROUND THE REAL WIRE. A person stands on a team two ways, and the two are
// not exclusive:
//   • DIRECT — a membership row written on the team itself, with the role set there.
//   • BY DESCENT — admin reaching the team from a team above it (an admin of a team is
//     an admin of every team beneath it; an admin of the org root is an admin
//     everywhere). Descent is not editable on the lower team.
// The wire decides both per row (`direct_role`, `descent_role`, `descent_from_team_*`,
// and the effective `role`), so nothing here recomputes descent. The two listings
// overlap: the org admin added to a sub-team appears in its direct listing (their row)
// AND its by-descent listing (from the root). The root's listing is unaffected by
// anything below it.

export type Role = "admin" | "member";

export const ROLE_LABEL: Record<Role, string> = { admin: "Admin", member: "Member" };

/** A count of the people holding a row on a team — its direct members. Shared by the tree's
 *  badges and the roster footer so the wording can't drift between them. Says "direct"
 *  because that is what the wire's `member_count` counts: admins reaching the team by
 *  descent hold no row there and are not in it. */
export function memberCountLabel(count: number): string {
  return `${count} direct ${count === 1 ? "member" : "members"}`;
}

/** The by-descent listing's tally. */
export function descentCountLabel(count: number): string {
  return `${count} by descent`;
}

export interface Person {
  id: string;
  name: string;
  email: string;
  initials: string;
}

export interface Team {
  id: string;
  name: string;
  parentId: string | null;
  isRoot?: boolean;
  /** People holding a row on this team (the wire's `member_count`). */
  memberCount: number;
}

export interface DescentSource {
  id: string;
  name: string;
}

/** One person's standing on one team, as the wire states it. */
export interface Standing {
  personId: string;
  /** The row written on this team; null when the person reaches it by descent alone. */
  directRole: Role | null;
  /** The team above whose admin row reaches this team; null when nothing does. */
  descentFrom: DescentSource | null;
  /** What the person holds here once descent is applied. */
  role: Role;
  /** A short human label for when the standing began (formatted from created_at). */
  joined: string;
}

export interface Invite {
  id: string;
  email: string;
  role: Role;
  /** A short human label for when the invitation lapses (formatted from expires_at). */
  expires: string;
}

/**
 * The org graph the page renders. Populated by the adapter or a fixture:
 *  - `teams` is the WHOLE org (every team, with its parent + its direct member count).
 *  - `standing` holds the selected team's roster — every person who stands on it, either
 *    way. It is intentionally absent for any other team.
 *  - `invites` is the selected team only.
 *  - `people` maps every id referenced by `standing` to its person.
 *  - `viewerId` / `orgName` / `rootId` are the live identity context.
 *  - `adminTeamIds` is every team the viewer administers, descent included (`/auth/me`) — what
 *    the page's controls are decided from (data/permissions.ts).
 */
export interface Graph {
  teams: Team[];
  people: Record<string, Person>;
  standing: Record<string, Standing[]>;
  invites: Record<string, Invite[]>;
  viewerId: string;
  orgName: string;
  rootId: string;
  adminTeamIds: string[];
}

/** The selected team's detail resolution. `forbidden` is first-class — the
 *  roster endpoint is team-admin gated, so a non-admin reaches a real "you don't
 *  manage this team" surface, never a blank pane or a raw 403. */
export type DetailStatus = "loading" | "ready" | "forbidden" | "error";

/** The seam's resolution. The page-level `status` gates on the team LIST (the
 *  tree); `detail` resolves the selected team's roster/invitations independently,
 *  so a forbidden roster still leaves the navigable tree intact. */
export interface TeamsState {
  status: "loading" | "error" | "ready";
  graph: Graph | null;
  detail: { status: DetailStatus; errorMessage: string | null };
  errorMessage: string | null;
  retry: () => void;
}

// ---- pure graph helpers (read the passed graph, never module state) --------

export function teamById(g: Graph, id: string): Team | undefined {
  return g.teams.find((t) => t.id === id);
}

export function childrenOf(g: Graph, teamId: string): Team[] {
  return g.teams.filter((t) => t.parentId === teamId).sort((a, b) => a.name.localeCompare(b.name));
}

/** Root-first chain of ancestors, excluding the team itself. */
export function ancestorsOf(g: Graph, teamId: string): Team[] {
  const chain: Team[] = [];
  let cur = teamById(g, teamId)?.parentId ?? null;
  while (cur) {
    const t = teamById(g, cur);
    if (!t) break;
    chain.unshift(t);
    cur = t.parentId;
  }
  return chain;
}

/** Every team strictly below this one (depth-first). */
export function descendantsOf(g: Graph, teamId: string): Team[] {
  const out: Team[] = [];
  for (const child of childrenOf(g, teamId)) out.push(child, ...descendantsOf(g, child.id));
  return out;
}

/** The people holding a row on a team — the wire's count, so it is right for every team in
 *  the org, not only the selected one. */
export function teamMemberCount(g: Graph, teamId: string): number {
  return teamById(g, teamId)?.memberCount ?? 0;
}

export interface RosterEntry {
  person: Person;
  /** The standing held here, descent applied. */
  role: Role;
  /** The row on this team, when there is one. */
  directRole: Role | null;
  /** Where admin reaches this team from, when it does. */
  descentFrom: DescentSource | null;
  /** Which listing the entry belongs to — one person can be in both. */
  listing: "direct" | "descent";
  joined: string;
}

function entry(g: Graph, s: Standing, listing: "direct" | "descent"): RosterEntry {
  return { person: g.people[s.personId], role: s.role, directRole: s.directRole, descentFrom: s.descentFrom, listing, joined: s.joined };
}

/** Direct members: everyone with a row on this team, with the role set there — editable
 *  here unless descent decides it. */
export function directRoster(g: Graph, teamId: string): RosterEntry[] {
  return (g.standing[teamId] ?? []).filter((s) => s.directRole !== null).map((s) => entry(g, s, "direct"));
}

/** Members by descent: everyone an admin row above reaches — read-only here, managed on the
 *  team they come from. Overlaps the direct listing when such a person also holds a row. */
export function descentRoster(g: Graph, teamId: string): RosterEntry[] {
  return (g.standing[teamId] ?? []).filter((s) => s.descentFrom !== null).map((s) => entry(g, s, "descent"));
}

export function directCount(g: Graph, teamId: string): number {
  return directRoster(g, teamId).length;
}

export function descentCount(g: Graph, teamId: string): number {
  return descentRoster(g, teamId).length;
}

export interface DescentAdmin {
  person: Person;
  fromTeamId: string;
  fromTeamName: string;
}

/** Permission descent: the admins of teams above who govern this team, each with the
 *  nearest team the standing comes from. */
export function descentAdmins(g: Graph, teamId: string): DescentAdmin[] {
  return descentRoster(g, teamId).map((e) => ({ person: e.person, fromTeamId: e.descentFrom!.id, fromTeamName: e.descentFrom!.name }));
}

/** The team's own admins (an admin row written here). */
export function directAdmins(g: Graph, teamId: string): Person[] {
  return directRoster(g, teamId)
    .filter((e) => e.directRole === "admin")
    .map((e) => e.person);
}

/** The team above whose admin row grants the viewer admin here — the parentage behind the
 *  viewer's power (null when the viewer holds an admin row here, or holds no admin here). */
export function viewerDescentSource(g: Graph, teamId: string): Team | null {
  const mine = (g.standing[teamId] ?? []).find((s) => s.personId === g.viewerId);
  if (!mine || mine.directRole === "admin" || !mine.descentFrom) return null;
  return teamById(g, mine.descentFrom.id) ?? { id: mine.descentFrom.id, name: mine.descentFrom.name, parentId: null, memberCount: 0 };
}

/** What else removing someone from `teamId` takes with it, in one sentence — or undefined when it
 *  takes nothing. The server removes the person from every team under `teamId`, and from each team
 *  above it (short of the org root) where no other membership of theirs remains. `self` words it
 *  for the viewer leaving. */
export function removalCascade(g: Graph, teamId: string, self: boolean): string | undefined {
  const team = teamById(g, teamId);
  if (!team) return undefined;
  const they = self ? "you" : "they";
  const parts: string[] = [];
  if (childrenOf(g, teamId).length > 0) parts.push(`every team under ${team.name}`);
  if (team.parentId && team.parentId !== g.rootId) parts.push(`any team above it ${they} belong to only through ${team.name}`);
  if (parts.length === 0) return undefined;
  return `${self ? "You" : "They"} also leave ${parts.join(", and ")}.`;
}

export function invitesFor(g: Graph, teamId: string): Invite[] {
  return g.invites[teamId] ?? [];
}

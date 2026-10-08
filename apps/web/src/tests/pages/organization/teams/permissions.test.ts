import { describe, expect, it } from "vitest";

import { removalCascade, type Graph, type Team } from "@/pages/organization/teams/data/model";
import {
  abilitiesOn,
  administeredTops,
  canRemove,
  isOrgAdmin,
  landingFor,
  moveTargets,
} from "@/pages/organization/teams/data/permissions";

// The page's controls follow the server's policy, read off `admin_team_ids`. Each answer here is the
// one a route would give: an admin of a team (descent included) manages it; only the root's admin
// restructures; nobody removes themself from the root; a move needs admin of the destination.

// root ─┬─ eng ── data
//       └─ ops
const team = (id: string, parentId: string | null): Team => ({ id, name: id[0].toUpperCase() + id.slice(1), parentId, isRoot: parentId === null, memberCount: 0 });
const TEAMS = [team("root", null), team("eng", "root"), team("data", "eng"), team("ops", "root")];

const graph = (adminTeamIds: string[]): Graph => ({
  teams: TEAMS,
  people: {},
  standing: {},
  invites: {},
  viewerId: "me",
  orgName: "Root",
  rootId: "root",
  adminTeamIds,
});

const ORG_ADMIN = graph(["root", "eng", "data", "ops"]);
const ENG_ADMIN = graph(["eng", "data"]);
const TWO_LEADS = graph(["eng", "data", "ops"]);
const MEMBER = graph([]);

describe("landing", () => {
  it.each([
    ["an org admin opens on the root", ORG_ADMIN, { kind: "team", teamId: "root" }],
    ["a sub-team admin opens on the team they lead, not the one below it", ENG_ADMIN, { kind: "team", teamId: "eng" }],
    ["someone who administers nothing opens on the root (the read-only surface)", MEMBER, { kind: "team", teamId: "root" }],
  ])("%s", (_name, g, expected) => {
    expect(landingFor(g)).toEqual(expected);
  });

  it("someone leading two unrelated teams is asked to choose, by name", () => {
    const landing = landingFor(TWO_LEADS);
    expect(landing.kind).toBe("pick");
    expect(landing.kind === "pick" ? landing.teams.map((t) => t.id) : null).toEqual(["eng", "ops"]);
  });

  it("the teams someone leads are their topmost admin teams only", () => {
    expect(administeredTops(ENG_ADMIN).map((t) => t.id)).toEqual(["eng"]);
    expect(administeredTops(MEMBER)).toEqual([]);
  });
});

describe("abilities", () => {
  it.each([
    ["org admin on a leaf", ORG_ADMIN, "data", { manage: true, restructure: true }],
    ["sub-team admin on their team", ENG_ADMIN, "eng", { manage: true, restructure: false }],
    ["sub-team admin on a team below by descent", ENG_ADMIN, "data", { manage: true, restructure: false }],
    ["sub-team admin on the root", ENG_ADMIN, "root", { manage: false, restructure: false }],
    ["sub-team admin on a sibling", ENG_ADMIN, "ops", { manage: false, restructure: false }],
    ["plain member anywhere", MEMBER, "eng", { manage: false, restructure: false }],
  ])("%s", (_name, g, teamId, expected) => {
    expect(abilitiesOn(g, teamId)).toEqual(expected);
  });

  it("org admin is admin of the root, never inferred from a lower team", () => {
    expect(isOrgAdmin(ORG_ADMIN)).toBe(true);
    expect(isOrgAdmin(ENG_ADMIN)).toBe(false);
  });

  it.each([
    ["an admin removes someone else from the root", ORG_ADMIN, "root", "dana", true],
    ["nobody removes themself from the root", ORG_ADMIN, "root", "me", false],
    ["leaving a sub-team is allowed", ORG_ADMIN, "eng", "me", true],
    ["a sub-team admin removes from their team", ENG_ADMIN, "data", "dana", true],
    ["a sub-team admin cannot remove from a team they don't administer", ENG_ADMIN, "ops", "dana", false],
  ])("%s", (_name, g, teamId, personId, expected) => {
    expect(canRemove(g, teamId, personId)).toBe(expected);
  });

  it("a member moves only to another team the viewer administers", () => {
    expect(moveTargets(ENG_ADMIN, "eng").map((t) => t.id)).toEqual(["data"]);
    expect(moveTargets(graph(["data"]), "data")).toEqual([]);
    expect(moveTargets(ORG_ADMIN, "eng").map((t) => t.id).sort()).toEqual(["data", "ops", "root"]);
  });
});

describe("removal cascade", () => {
  it.each([
    ["a leaf under the root takes nothing else", "ops", false, undefined],
    ["a team with sub-teams takes them too", "eng", false, "They also leave every team under Eng."],
    ["a leaf under a sub-team may take the team above", "data", false, "They also leave any team above it they belong to only through Data."],
    ["the viewer leaving reads in the second person", "data", true, "You also leave any team above it you belong to only through Data."],
  ])("%s", (_name, teamId, self, expected) => {
    expect(removalCascade(ORG_ADMIN, teamId, self)).toBe(expected);
  });
});

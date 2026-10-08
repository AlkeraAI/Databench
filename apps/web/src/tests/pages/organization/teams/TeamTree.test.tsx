import { MemoryRouter } from "react-router-dom";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TeamTree } from "@/pages/organization/teams/overlays/TeamTree";
import { memberCountLabel, type Graph, type Team } from "@/pages/organization/teams/data/model";

// Each tree row shows its member count as a bare number, so the count only reaches a screen reader
// through the row's aria-label. Two things are pinned: the label's grammar (one member is singular,
// anything else plural, including zero) and its honesty (it counts the rows on the team — "direct"),
// and that the tree renders that same shared formatter rather than its own wording — a row that
// hard-codes "N members" announces "1 members" to one member.

/** An org of one team holding `direct` rows, plus a sub-team holding `nested` rows. Each team's
 *  reading is ITS OWN row count from the wire — a sub-team's rows are materialized onto the root by
 *  the backend, so the root's wire count already includes them; nothing is summed here. */
function graph(direct: number, nested = 0): Graph {
  const teams: Team[] = [{ id: "root", name: "Tideline", parentId: null, isRoot: true, memberCount: direct + nested }];
  if (nested > 0) teams.push({ id: "sub", name: "Platform", parentId: "root", isRoot: false, memberCount: nested });
  return {
    teams,
    people: {},
    standing: {},
    invites: {},
    viewerId: "r0",
    orgName: "Tideline",
    rootId: "root",
    adminTeamIds: teams.map((t) => t.id),
  };
}

const renderTree = (g: Graph) =>
  render(
    <MemoryRouter>
      <TeamTree graph={g} selectedId="root" expanded={new Set(["root"])} onSelect={vi.fn()} onToggle={vi.fn()} />
    </MemoryRouter>,
  );

afterEach(cleanup);

describe("memberCountLabel", () => {
  // The asymmetric case is the point: only exactly one is singular. Zero and two-plus are plural.
  it.each([
    [0, "0 direct members"],
    [1, "1 direct member"],
    [2, "2 direct members"],
  ])("reads %i as %s", (count, expected) => {
    expect(memberCountLabel(count)).toBe(expected);
  });
});

describe("TeamTree member counts", () => {
  // Grammar is the sweep above's job. These two renders only have to prove the tree reaches for
  // the shared formatter, which takes one singular and one plural reading.
  it.each([[1], [2]])("names the row's count for %i member(s)", (count) => {
    renderTree(graph(count));
    // The label comes from the shared formatter, so the tree's wording can't drift from the roster's.
    const reading = screen.getByLabelText(memberCountLabel(count));
    expect(reading).toHaveTextContent(String(count));
  });

  it("reads each team's own row count — the root's includes the rows materialized from below", () => {
    // One row of the root's own plus one materialized from Platform: the parent announces two, the child one.
    renderTree(graph(1, 1));
    expect(screen.getByLabelText(memberCountLabel(2))).toHaveTextContent("2");
    expect(screen.getByLabelText(memberCountLabel(1))).toHaveTextContent("1");
  });
});

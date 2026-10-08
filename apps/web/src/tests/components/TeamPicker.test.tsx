// The owner picker: "Just me" above the org's team tree, only the teams the
// caller administers selectable.
//
// The point of the control is that the refused teams STAY on screen — a tree
// with its unselectable branches removed is no longer the shape of the org the
// person is picking inside — so every case below is about a row that is visible
// and does not answer, as much as about the rows that do.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  ME_NODE_ID,
  NOT_AN_ADMIN_REASON,
  TeamPicker,
  type PickableTeam,
  type TeamPickerValue,
} from "@/components/TeamPicker";

const ROOT = "11111111-1111-1111-1111-111111111111";
const SUB = "22222222-2222-2222-2222-222222222222";
const DEEP = "33333333-3333-3333-3333-333333333333";
const SIBLING = "44444444-4444-4444-4444-444444444444";

/** An org four teams deep and two wide, so descent and siblings are both real. */
const TEAMS: PickableTeam[] = [
  { id: ROOT, name: "Acme", parent_team_id: null, is_root: true },
  { id: SUB, name: "Analytics", parent_team_id: ROOT },
  { id: DEEP, name: "Reporting", parent_team_id: SUB },
  { id: SIBLING, name: "Billing", parent_team_id: ROOT },
];

function setup(
  over: {
    value?: TeamPickerValue;
    adminTeamIds?: string[];
    teams?: PickableTeam[];
    disabled?: boolean;
  } = {},
) {
  const onChange = vi.fn();
  render(
    <TeamPicker
      value={over.value ?? { kind: "me" }}
      onChange={onChange}
      teams={over.teams ?? TEAMS}
      adminTeamIds={new Set(over.adminTeamIds ?? [SUB, DEEP])}
      disabled={over.disabled}
    />,
  );
  return { onChange };
}

// A treeitem's accessible name is computed from its contents, so a parent's
// name contains every descendant's. Both helpers below go through the row's own
// label text instead, which names exactly one row.

/** The clickable line inside a treeitem — what a pointer user actually hits. */
function row(label: string): HTMLElement {
  return screen.getByText(label).closest(".alk-tree__row") as HTMLElement;
}

/** The focusable treeitem carrying that label — where the aria state lives. */
function item(label: string): HTMLElement {
  return screen.getByText(label).closest('[role="treeitem"]') as HTMLElement;
}

describe("TeamPicker", () => {
  it("offers the person themselves above their whole org", () => {
    setup();
    const names = screen.getAllByRole("treeitem").map((el) => el.textContent ?? "");
    // Every team is on screen, and the personal row is the first thing read.
    expect(names[0]).toMatch(/Just me/);
    for (const team of TEAMS) {
      expect(screen.getByText(team.name)).toBeInTheDocument();
    }
  });

  it("answers with the person when their own row is picked", async () => {
    const { onChange } = setup({ value: { kind: "team", teamId: SUB } });
    await userEvent.click(row("Just me"));
    expect(onChange).toHaveBeenCalledWith({ kind: "me" });
  });

  it("answers with the team when one they administer is picked", async () => {
    const { onChange } = setup();
    await userEvent.click(row("Reporting"));
    expect(onChange).toHaveBeenCalledWith({ kind: "team", teamId: DEEP });
  });

  it("shows a team they don't administer, refuses it, and says why", async () => {
    const { onChange } = setup();
    // Billing is a leaf they hold no admin on: visible, marked refused, and the
    // reason is on the row rather than in a toast after the click.
    expect(item("Billing")).toHaveAttribute("aria-disabled", "true");
    expect(row("Billing")).toHaveAttribute("title", NOT_AN_ADMIN_REASON);
    await userEvent.click(row("Billing"));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("refuses a team above the one they administer", async () => {
    // Admin descends, so administering Analytics says nothing about Acme above
    // it. A picker that let them pick the root would offer a save the server
    // then refuses.
    const { onChange } = setup();
    await userEvent.click(row("Acme"));
    expect(onChange).not.toHaveBeenCalled();
    expect(item("Acme")).toHaveAttribute("aria-disabled", "true");
  });

  it("marks exactly the administered teams as selectable", () => {
    setup();
    const selectable = screen
      .getAllByRole("treeitem")
      .filter((el) => !el.hasAttribute("aria-disabled"))
      .map((el) => (el.querySelector(".alk-tree__row") as HTMLElement).textContent ?? "");
    // The personal row plus the two administered teams — and nothing else, which
    // is the assertion a positive-only check would miss.
    expect(selectable).toHaveLength(3);
    expect(selectable.some((t) => t.includes("Just me"))).toBe(true);
    expect(selectable.some((t) => t.includes("Analytics"))).toBe(true);
    expect(selectable.some((t) => t.includes("Reporting"))).toBe(true);
  });

  it("is navigable and selectable from the keyboard", async () => {
    const { onChange } = setup();
    const target = item("Reporting");
    target.focus();
    await userEvent.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledWith({ kind: "team", teamId: DEEP });
  });

  it("does not answer for a refused row from the keyboard either", async () => {
    const { onChange } = setup();
    item("Billing").focus();
    await userEvent.keyboard("{Enter}");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("marks the current answer as the selected row", () => {
    setup({ value: { kind: "team", teamId: SUB } });
    expect(item("Analytics")).toHaveAttribute("aria-selected", "true");
    expect(item("Just me")).toHaveAttribute("aria-selected", "false");
  });

  it("refuses everything while disabled, the personal row included", async () => {
    const { onChange } = setup({ disabled: true, adminTeamIds: [SUB] });
    await userEvent.click(row("Just me"));
    await userEvent.click(row("Analytics"));
    expect(onChange).not.toHaveBeenCalled();
    // A row refused because the whole control is refused carries no per-row
    // reason — "you aren't an admin" would be a lie about a team they administer.
    expect(row("Analytics")).not.toHaveAttribute("title");
  });

  it("shows the personal row alone when the org has no teams to offer", () => {
    setup({ teams: [] });
    expect(screen.getAllByRole("treeitem")).toHaveLength(1);
    expect(item("Just me")).toBeInTheDocument();
  });

  it("places a team under its parent, not beside it", () => {
    setup();
    // Reporting is inside Analytics, which is inside Acme — the three levels the
    // aria model has to carry for a screen reader to read the org's shape.
    expect(item("Acme")).toHaveAttribute("aria-level", "1");
    expect(item("Analytics")).toHaveAttribute("aria-level", "2");
    expect(item("Reporting")).toHaveAttribute("aria-level", "3");
    expect(within(item("Acme")).getByText("Analytics")).toBeInTheDocument();
    expect(within(item("Analytics")).getByText("Reporting")).toBeInTheDocument();
  });

  it("drops a team whose parent is not in the list rather than orphaning it", () => {
    // One unresolvable row is a data problem; it must not take the rest of the
    // tree — or the dialog it sits in — down with it.
    setup({
      teams: [
        { id: ROOT, name: "Acme", parent_team_id: null, is_root: true },
        { id: SUB, name: "Analytics", parent_team_id: ROOT },
        { id: DEEP, name: "Orphan", parent_team_id: "00000000-0000-0000-0000-00000000dead" },
      ],
    });
    expect(screen.getByText("Analytics")).toBeInTheDocument();
    expect(screen.queryByText("Orphan")).toBeNull();
  });

  it("names the personal row whatever the surface calls it", () => {
    render(
      <TeamPicker
        value={{ kind: "me" }}
        onChange={vi.fn()}
        teams={[]}
        adminTeamIds={new Set()}
        meLabel="Nobody else"
      />,
    );
    expect(screen.getByText("Nobody else")).toBeInTheDocument();
    expect(ME_NODE_ID).not.toMatch(/^[0-9a-f]{8}-/); // never collides with a team id
  });
});

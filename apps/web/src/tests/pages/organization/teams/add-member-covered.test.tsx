import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AddMemberModal } from "@/pages/organization/teams/overlays/modals";
import type { Person } from "@/pages/organization/teams/data/model";

// Someone reaching the team as admin by descent holds no row on it, so the dialog still offers
// them — but descent decides their role: the pick is marked as covered, the role choice is locked to
// admin with the fact stated, and the submit carries admin whatever the admin had picked before.
// A plain org member keeps the free choice. Driven through the real dialog with its props only.

const person = (id: string, name: string): Person => ({ id, name, email: `${id}@x.io`, initials: name.slice(0, 2).toUpperCase() });
const DIRECTORY = [person("vera", "Vera Ng"), person("dana", "Dana Whitfield")];

afterEach(cleanup);

function setup() {
  const onSubmit = vi.fn();
  render(
    <AddMemberModal
      open
      directory={DIRECTORY}
      loading={false}
      excludeIds={new Set()}
      coveredBy={new Map([["vera", "Tideline"]])}
      pendingInvites={[]}
      teamName="Test"
      onClose={vi.fn()}
      onSubmit={onSubmit}
      onInvite={vi.fn(async () => null)}
    />,
  );
  return { onSubmit };
}

describe("AddMemberModal — a pick covered by descent", () => {
  it("marks the covered candidate with the team the standing comes from", () => {
    setup();
    const people = screen.getByRole("group", { name: "People" });
    expect(within(people).getByText(/admin here by descent from tideline/i)).toBeInTheDocument();
    // The uncovered candidate carries no such mark.
    expect(within(people).getByText("dana@x.io")).toBeInTheDocument();
  });

  it("locks the role to admin once a covered candidate is picked, and submits admin", async () => {
    const user = userEvent.setup();
    const { onSubmit } = setup();
    // The admin had chosen Member first — the lock must override that, not merely default.
    await user.click(screen.getByRole("radio", { name: /^member/i }));
    await user.click(screen.getByRole("button", { name: /vera ng/i }));

    const group = screen.getByRole("radiogroup", { name: "Role" });
    expect(group).toHaveAttribute("aria-disabled", "true");
    for (const radio of within(group).getAllByRole("radio")) expect(radio).toBeDisabled();
    expect(within(group).getByRole("radio", { name: /admin/i })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/by descent from tideline/i, { selector: "p" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Add as direct admin" }));
    expect(onSubmit).toHaveBeenCalledWith("vera", "admin");
  });

  it("leaves the choice free for a candidate nothing reaches from above", async () => {
    const user = userEvent.setup();
    const { onSubmit } = setup();
    await user.click(screen.getByRole("button", { name: /dana whitfield/i }));
    const group = screen.getByRole("radiogroup", { name: "Role" });
    expect(group).not.toHaveAttribute("aria-disabled");
    await user.click(within(group).getByRole("radio", { name: /admin/i }));
    await user.click(screen.getByRole("button", { name: "Add member" }));
    expect(onSubmit).toHaveBeenCalledWith("dana", "admin");
  });
});

// The same dialog is a people picker that falls through to invite-by-email, so what it
// says about text matching nobody has to tell a search that came up empty apart from an
// address the admin got wrong.
describe("AddMemberModal — text that matches nobody", () => {
  // The picker's box is a controlled `<input type="search">`; jsdom + userEvent.type drops
  // keystrokes on one, so the query is set with the change a typist would produce.
  const search = (text: string) =>
    fireEvent.change(screen.getByRole("searchbox", { name: /search people/i }), {
      target: { value: text },
    });

  it("names the missing address shape when the text is not an email", () => {
    setup();
    search("not-an-email");
    expect(screen.getByText("Enter a full email address to invite someone.")).toBeInTheDocument();
    expect(screen.queryByText("No one matches your search.")).not.toBeInTheDocument();
  });

  it("says the same for a name-shaped query nobody answers", () => {
    // A name that finds nobody leaves the admin in the same place: the only way on
    // from here is an address, so the dialog names it rather than reporting a miss.
    setup();
    search("Quentin");
    expect(screen.getByText("Enter a full email address to invite someone.")).toBeInTheDocument();
  });

  it("keeps its own sentence when nothing has been typed at all", () => {
    // Nothing typed is not a failed search — it means the team already holds everyone.
    render(
      <AddMemberModal
        open
        directory={DIRECTORY}
        loading={false}
        excludeIds={new Set(DIRECTORY.map((p) => p.id))}
        coveredBy={new Map()}
        pendingInvites={[]}
        teamName="Test"
        onClose={vi.fn()}
        onSubmit={vi.fn()}
        onInvite={vi.fn(async () => null)}
      />,
    );
    expect(
      screen.getByText("Everyone in your organization is already a direct member of this team."),
    ).toBeInTheDocument();
  });

  it("offers the invite once the text is a whole address", () => {
    setup();
    search("newcomer@x.io");
    expect(screen.getByText("newcomer@x.io")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send invite" })).toBeInTheDocument();
    expect(screen.queryByText("Enter a full email address to invite someone.")).not.toBeInTheDocument();
  });
});

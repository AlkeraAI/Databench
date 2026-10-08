import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Toolbar } from "@/pages/organization/teams/detail/Toolbar";

// The roster toolbar after its scope switch moved onto the ui SegmentedControl. These assert
// what a user / AT can observe — the two scope tabs with their counts, which is selected, that a
// click changes the scope, that the search routes through, and that the scope switch is absent when
// the team inherits nothing — never the library's class names.

afterEach(cleanup);

function setup(over: Partial<Parameters<typeof Toolbar>[0]> = {}) {
  const onScope = vi.fn();
  const onQuery = vi.fn();
  render(
    <Toolbar
      hasScope
      scope="direct"
      onScope={onScope}
      directCount={4}
      descentCount={9}
      query=""
      onQuery={onQuery}
      searchLabel="Find a member in Platform"
      {...over}
    />,
  );
  return { onScope, onQuery };
}

describe("Toolbar scope switch", () => {
  it("renders both scope tabs, each carrying its count", () => {
    setup();
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(2);

    const direct = screen.getByRole("tab", { name: /direct/i });
    const all = screen.getByRole("tab", { name: /by descent/i });
    expect(within(direct).getByText("4")).toBeInTheDocument();
    expect(within(all).getByText("9")).toBeInTheDocument();
  });

  it("marks the active scope selected and the other not", () => {
    setup({ scope: "descent" });
    expect(screen.getByRole("tab", { name: /by descent/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /direct/i })).toHaveAttribute("aria-selected", "false");
  });

  it("fires onScope with the option key when a tab is clicked", async () => {
    const user = userEvent.setup();
    const { onScope } = setup({ scope: "direct" });
    await user.click(screen.getByRole("tab", { name: /by descent/i }));
    expect(onScope).toHaveBeenCalledWith("descent");
  });

  it("hides the scope switch when nothing reaches the team from above", () => {
    setup({ hasScope: false });
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Find a member in Platform" })).toBeInTheDocument();
  });

  it("routes typed queries through onQuery", async () => {
    const user = userEvent.setup();
    const { onQuery } = setup();
    await user.type(screen.getByRole("searchbox", { name: "Find a member in Platform" }), "a");
    expect(onQuery).toHaveBeenCalledWith("a");
  });
});

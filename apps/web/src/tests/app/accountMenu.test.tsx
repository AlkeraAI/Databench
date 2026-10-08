// Where a person finds their OWN preferences.
//
// They are nobody's but the reader's, so they are not an Organization nav entry
// beside the org's ledger — they sit in the account menu next to Profile. Both
// halves are pinned here because either one alone is a dead end: an entry the
// nav still carries would put a personal page under the org's heading, and a
// menu item that navigates nowhere (or to the old path) is a control nobody can
// reach.

import { MemoryRouter, Route, Routes } from "react-router-dom";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NAV, type NavLeaf } from "@/app/nav";

vi.mock("@/api/auth", () => ({
  useCurrentUser: () => ({ data: { display_name: "Vera Ng", email: "vera@x.io" } }),
  useLogout: () => ({ mutate: vi.fn(), isPending: false }),
  useMemberships: () => ({ data: undefined }),
  useSwitchOrg: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/api/dashboard", () => ({
  useCurrentUser: () => ({ data: undefined }),
  useIdentityDashboard: () => ({ data: { is_org_admin: true } }),
  useMyCredits: () => ({ data: { tier_name: "Pro" } }),
}));
vi.mock("@/api/config", () => ({ usePublicConfig: () => ({ data: { self_hosted: false } }) }));
vi.mock("@/api/events/RealtimeStatusIndicator", () => ({ RealtimeStatusIndicator: () => null }));

import { AppLayout } from "@/app/AppLayout";

/** The shell with two stand-in destinations, so a menu pick is a REAL navigation
 *  through the router rather than a spied callback. */
function renderShell() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route element={<AppLayout />}>
          <Route path="/" element={<p>the overview</p>} />
          <Route path="/preferences" element={<p>the preferences page</p>} />
          <Route path="/settings/organization" element={<p>the org ledger</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

const leaves = (): NavLeaf[] =>
  NAV.flatMap((group) =>
    group.items.flatMap((item) => ("children" in item && item.children ? item.children : [item as NavLeaf])),
  );

afterEach(cleanup);

describe("the primary nav", () => {
  it("carries no personal-preferences entry", () => {
    expect(leaves().map((leaf) => leaf.label)).not.toContain("Chat preferences");
    expect(leaves().map((leaf) => leaf.to)).not.toContain("/settings/chat");
    expect(leaves().map((leaf) => leaf.to)).not.toContain("/preferences");
  });

  it("does not render a Chat preferences doorway in the rail", () => {
    renderShell();
    expect(screen.queryByRole("link", { name: /chat preferences/i })).toBeNull();
  });
});

describe("the account menu", () => {
  const open = async () => {
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /vera ng/i }));
    return user;
  };

  it("offers Preferences beside Profile", async () => {
    renderShell();
    await open();

    const items = screen
      .getAllByRole("menuitem")
      .map((item) => item.textContent?.trim() ?? "")
      .filter((text) => ["Profile", "Preferences", "Settings"].includes(text));
    // Beside Profile, and BEFORE the organization's own Settings — a personal
    // page filed under the org's heading is the mistake this ordering prevents.
    expect(items).toEqual(["Profile", "Preferences", "Settings"]);
  });

  it("navigates to /preferences, and names the page Preferences in the masthead", async () => {
    renderShell();
    const user = await open();

    await user.click(screen.getByRole("menuitem", { name: "Preferences" }));

    expect(await screen.findByText("the preferences page")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Preferences");
  });

  // It looks and behaves like a menu, so a screen reader was given a generic group
  // where every other menu in the product announces a menu with rows.
  it("announces itself as a menu, with every row a menu item", async () => {
    renderShell();
    await open();

    const menu = screen.getByRole("menu", { name: "Account" });
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent?.trim())).toEqual([
      "Profile",
      "Preferences",
      "Settings",
      "Sign out",
    ]);
    // The theme choice is a set of options within the menu, not a radiogroup — a radio
    // is not a valid child of a menu.
    const theme = within(menu).getByRole("group", { name: "Theme" });
    expect(within(theme).getAllByRole("menuitemradio")).toHaveLength(3);
    expect(within(menu).queryByRole("radiogroup")).toBeNull();
  });

  it("walks its rows with the arrow keys, wrapping at both ends", async () => {
    // A container claiming role="menu" promises arrow-key navigation; one a keyboard
    // user can only Tab through is worse than one that never claimed to be a menu.
    renderShell();
    const user = await open();
    const menu = screen.getByRole("menu", { name: "Account" });
    const rows = [...menu.querySelectorAll<HTMLElement>('[role^="menuitem"]')];

    expect(rows.length).toBeGreaterThan(3);
    await user.keyboard("{Home}");
    expect(document.activeElement).toBe(rows[0]);
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(rows[1]);
    await user.keyboard("{ArrowUp}{ArrowUp}");
    expect(document.activeElement).toBe(rows[rows.length - 1]);
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(rows[0]);
    await user.keyboard("{End}");
    expect(document.activeElement).toBe(rows[rows.length - 1]);
  });

  it("hands focus back to the account button when Escape closes it", async () => {
    // Walked with the keyboard alone: Enter opens it, the arrows move through it, and Escape
    // must leave the reader on the control they opened it from — not on the page body.
    renderShell();
    const user = userEvent.setup();
    const account = screen.getByRole("button", { name: /vera ng/i });
    account.focus();
    await user.keyboard("{Enter}");
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(screen.getByRole("menu", { name: "Account" }).contains(document.activeElement)).toBe(true);

    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("menu", { name: "Account" })).toBeNull());
    expect(document.activeElement).toBe(account);
  });
});

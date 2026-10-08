import { MemoryRouter, Route, Routes } from "react-router-dom";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

// The org switcher in the account menu, and the active org in the shell.
//
// A person in several orgs sees which org this browser is in under their name, and nowhere else
// in the shell. The account menu has one "Switch organization" row whose flyout opens beside it on
// hover, click or the keyboard and lists every org: the one this browser is in greyed and marked
// current, the others offered. A person in one org sees no org line and no switcher, even if a
// memberships read somehow answered with more than one org.

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const ORG_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

const state = {
  membershipCount: 1,
  memberships: [
    { org_team_id: ORG_B, org_name: "Beta Labs", role: "member", sso_required: false },
    { org_team_id: ORG_A, org_name: "Acme", role: "admin", sso_required: false },
    { org_team_id: ORG_C, org_name: "Corp", role: "member", sso_required: true },
  ],
  enabledAsked: [] as (boolean | undefined)[],
};
const switchMutate = vi.fn();

vi.mock("@/api/auth", () => ({
  useCurrentUser: () => ({
    data: {
      display_name: "Vera Ng",
      email: "vera@x.io",
      org_team_id: ORG_A,
      org_name: "Acme",
      membership_count: state.membershipCount,
    },
  }),
  useLogout: () => ({ mutate: vi.fn(), isPending: false }),
  useMemberships: (options?: { enabled?: boolean }) => {
    state.enabledAsked.push(options?.enabled);
    return { data: { active_org_team_id: ORG_A, memberships: state.memberships } };
  },
  useSwitchOrg: () => ({ mutate: switchMutate, isPending: false }),
}));
vi.mock("@/api/dashboard", () => ({
  useIdentityDashboard: () => ({ data: { is_org_admin: true } }),
  useMyCredits: () => ({ data: { tier_name: "Pro" } }),
}));
vi.mock("@/api/config", () => ({ usePublicConfig: () => ({ data: { self_hosted: false } }) }));
vi.mock("@/api/events/RealtimeStatusIndicator", () => ({ RealtimeStatusIndicator: () => null }));

const { AppLayout } = await import("@/app/AppLayout");

function renderShell() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route element={<AppLayout />}>
          <Route path="/" element={<p>the overview</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

async function openMenu() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /vera ng/i }));
  return { user, menu: screen.getByRole("menu", { name: "Account" }) };
}

afterEach(() => {
  cleanup();
  state.membershipCount = 1;
  state.enabledAsked = [];
  switchMutate.mockReset();
});

describe("a person in one org", () => {
  it("sees no org line, no sidebar org and no switcher, and fetches no memberships", async () => {
    renderShell();
    expect(screen.queryByText(/Acme/)).toBeNull();
    const { menu } = await openMenu();
    expect(within(menu).queryByRole("menuitem", { name: "Switch organization" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Beta Labs" })).toBeNull();
    expect(state.enabledAsked.every((enabled) => enabled === false)).toBe(true);
  });
});

describe("a person in several orgs", () => {
  it("sees the active org under their name and not in the sidebar header", () => {
    state.membershipCount = 3;
    const { container } = renderShell();
    expect(screen.getByRole("button", { name: /vera ng/i })).toHaveTextContent("Acme · Org admin");
    const head = container.querySelector(".alk-sidebar__head");
    expect(head).not.toBeNull();
    expect(head).not.toHaveTextContent("Acme");
    // The account button is the one place in the sidebar that names the org.
    const sidebar = container.querySelector("aside.alk-sidebar") as HTMLElement;
    expect(within(sidebar).getAllByText(/Acme/)).toHaveLength(1);
  });

  it("has one Switch organization row and lists no org inline in the menu", async () => {
    state.membershipCount = 3;
    renderShell();
    const { menu } = await openMenu();
    const row = within(menu).getByRole("menuitem", { name: "Switch organization" });
    expect(row).toHaveAttribute("aria-haspopup", "menu");
    expect(within(menu).queryByRole("menuitem", { name: "Beta Labs" })).toBeNull();
    const rows = within(menu).getAllByRole("menuitem").map((item) => item.textContent?.trim());
    expect(rows.indexOf("Switch organization")).toBeLessThan(rows.indexOf("Sign out"));
  });

  it("hovering the row lists every org, the current one disabled and marked current", async () => {
    state.membershipCount = 3;
    renderShell();
    const { user, menu } = await openMenu();
    await user.hover(within(menu).getByRole("menuitem", { name: "Switch organization" }));
    const flyout = await screen.findByRole("menu", { name: "Switch organization" });
    expect(within(flyout).getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "Beta Labs",
      "Acme",
      "Corp",
    ]);
    const current = within(flyout).getByRole("menuitem", { name: "Acme" });
    expect(current).toBeDisabled();
    expect(current).toHaveAttribute("aria-current", "true");
    expect(within(flyout).getByRole("menuitem", { name: "Corp" })).toBeEnabled();
    // The account menu stays open behind the flyout.
    expect(screen.getByRole("menu", { name: "Account" })).toBeInTheDocument();
  });

  it("switches to the org picked in the flyout", async () => {
    state.membershipCount = 3;
    renderShell();
    const { user, menu } = await openMenu();
    await user.click(within(menu).getByRole("menuitem", { name: "Switch organization" }));
    const flyout = await screen.findByRole("menu", { name: "Switch organization" });
    await user.click(within(flyout).getByRole("menuitem", { name: "Corp" }));
    expect(switchMutate).toHaveBeenCalledWith({ orgTeamId: ORG_C });
  });

  it("does nothing on a press on the current org", async () => {
    state.membershipCount = 3;
    renderShell();
    const { user, menu } = await openMenu();
    await user.click(within(menu).getByRole("menuitem", { name: "Switch organization" }));
    const flyout = await screen.findByRole("menu", { name: "Switch organization" });
    await user.click(within(flyout).getByRole("menuitem", { name: "Acme" }));
    expect(switchMutate).not.toHaveBeenCalled();
  });

  it("ArrowRight opens the flyout with focus on the first other org", async () => {
    state.membershipCount = 3;
    renderShell();
    const { user, menu } = await openMenu();
    within(menu).getByRole("menuitem", { name: "Switch organization" }).focus();
    await user.keyboard("{ArrowRight}");
    const flyout = await screen.findByRole("menu", { name: "Switch organization" });
    await waitFor(() => expect(document.activeElement).toBe(within(flyout).getByRole("menuitem", { name: "Beta Labs" })));
    // The current org is skipped by the arrows.
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(within(flyout).getByRole("menuitem", { name: "Corp" }));
  });

  it.each(["{Escape}", "{ArrowLeft}"])(
    "%s closes the flyout back to its row and keeps the account menu open",
    async (key) => {
      state.membershipCount = 3;
      renderShell();
      const { user, menu } = await openMenu();
      const row = within(menu).getByRole("menuitem", { name: "Switch organization" });
      row.focus();
      await user.keyboard("{ArrowRight}");
      await screen.findByRole("menu", { name: "Switch organization" });
      await user.keyboard(key);
      expect(row).toHaveAttribute("aria-expanded", "false");
      expect(document.activeElement).toBe(row);
      expect(screen.getByRole("menu", { name: "Account" })).toBeInTheDocument();
    },
  );
});

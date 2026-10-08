// Bypass blocks: a keyboard reader lands in the sidebar on every navigation and re-tabs the whole
// nav before reaching the page. The shell owes them one stop, first in the order, that jumps past it.

import { MemoryRouter, Route, Routes } from "react-router-dom";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/auth", () => ({
  useCurrentUser: () => ({ data: { display_name: "Vera Ng", email: "vera@x.io" } }),
  useLogout: () => ({ mutate: vi.fn(), isPending: false }),
  useMemberships: () => ({ data: undefined }),
  useSwitchOrg: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/api/dashboard", () => ({
  useCurrentUser: () => ({ data: undefined }),
  useIdentityDashboard: () => ({ data: { is_org_admin: false } }),
  useMyCredits: () => ({ data: { tier_name: "Pro" } }),
}));
vi.mock("@/api/config", () => ({ usePublicConfig: () => ({ data: { self_hosted: false } }) }));
vi.mock("@/api/events/RealtimeStatusIndicator", () => ({ RealtimeStatusIndicator: () => null }));

import { AppLayout } from "@/app/AppLayout";

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

beforeEach(() => {
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("the shell's skip link", () => {
  it("is the first thing the keyboard reaches on a signed-in page", async () => {
    const user = userEvent.setup();
    renderShell();

    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "Skip to content" }));
  });

  it("points at the page's own main region", () => {
    renderShell();
    const skip = screen.getByRole("link", { name: "Skip to content" });
    expect(skip).toHaveAttribute("href", "#main");
    const main = screen.getByRole("main");
    expect(main).toHaveAttribute("id", "main");
    // The target has to be able to take focus, or the jump moves the scroll but not the tab order.
    expect(main).toHaveAttribute("tabindex", "-1");
  });

  it("moves focus into main when activated", async () => {
    const user = userEvent.setup();
    renderShell();

    await user.tab();
    await user.keyboard("{Enter}");
    expect(document.activeElement).toBe(screen.getByRole("main"));
  });

  it("is hidden from sight until it is focused", () => {
    renderShell();
    const skip = screen.getByRole("link", { name: "Skip to content" });
    // The class that carries the visually-hidden-until-focused treatment, so the link never paints
    // over the masthead while the reader is using a mouse.
    expect(skip.className).toContain("alk-skip");
  });
});

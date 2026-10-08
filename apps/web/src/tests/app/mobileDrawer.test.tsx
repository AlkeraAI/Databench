// Narrow, the sidebar is the page's only navigation and opens as a drawer over a scrim. It has to
// behave as a modal: focus moves into it, Tab stays inside it, Escape closes it and focus returns
// to Open menu, and the page under the scrim is out of reach until it closes.

import { MemoryRouter, Route, Routes } from "react-router-dom";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
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

function stubWidth(narrow: boolean): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: narrow && query.includes("max-width: 900px"),
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

function renderShell() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route element={<AppLayout />}>
          <Route
            path="/"
            element={
              <button type="button" data-testid="behind">
                Start a chat
              </button>
            }
          />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

async function openDrawer() {
  const user = userEvent.setup();
  renderShell();
  const trigger = screen.getByRole("button", { name: "Open menu" });
  trigger.focus();
  await user.keyboard("{Enter}");
  const drawer = await screen.findByRole("dialog", { name: "Menu" });
  await waitFor(() => expect(drawer.contains(document.activeElement)).toBe(true));
  return { user, trigger, drawer };
}

beforeEach(() => {
  window.localStorage.clear();
  stubWidth(true);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("the narrow navigation drawer", () => {
  it("is a labelled modal dialog while open, and takes focus", async () => {
    const { drawer } = await openDrawer();
    expect(drawer).toHaveAttribute("aria-modal", "true");
    expect(drawer.tagName).toBe("ASIDE");
  });

  it("keeps Tab inside itself and never reaches the page behind the scrim", async () => {
    const { user, drawer } = await openDrawer();
    for (let press = 0; press < 12; press += 1) {
      await user.tab();
      expect(drawer.contains(document.activeElement), `Tab ${press + 1} left the drawer`).toBe(true);
    }
    await user.tab({ shift: true });
    expect(drawer.contains(document.activeElement)).toBe(true);
  });

  it("makes the page behind inert while it is open, and gives it back on close", async () => {
    const { user } = await openDrawer();
    expect(screen.getByRole("main", { hidden: true })).toHaveAttribute("inert");
    await user.keyboard("{Escape}");
    expect(screen.getByRole("main")).not.toHaveAttribute("inert");
  });

  it("closes on Escape and returns focus to Open menu", async () => {
    const { user, trigger } = await openDrawer();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Menu" })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("returns focus to Open menu when closed with its own Close menu button", async () => {
    const { user, trigger, drawer } = await openDrawer();
    await user.click(within(drawer).getByRole("button", { name: "Close menu" }));
    expect(screen.queryByRole("dialog", { name: "Menu" })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("is plain navigation, not a dialog, on a wide screen", () => {
    stubWidth(false);
    renderShell();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("main")).not.toHaveAttribute("inert");
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  });
});

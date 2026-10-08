// The rail the reader folded away stays folded away.
//
// Collapsing the shell's nav is an arrangement of the reader's own screen, not
// a per-visit choice: finding it open again after a reload is the shell
// forgetting something it was told. One answer for the whole product, kept in
// the browser, because a 13" laptop and a 34" monitor want different ones from
// the same account.

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

/** The shell marks the collapsed rail on the grid it lays out. */
function railCollapsed(): boolean {
  return document.querySelector(".alk-app")?.hasAttribute("data-collapsed") === true;
}

beforeEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("the app sidebar", () => {
  it("comes back collapsed on the next visit", async () => {
    const user = userEvent.setup();
    const first = renderShell();
    expect(railCollapsed()).toBe(false);

    await user.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(railCollapsed()).toBe(true);
    first.unmount();

    // A fresh mount is the next visit: nothing but the browser carried this.
    renderShell();
    expect(railCollapsed()).toBe(true);
    expect(screen.getByRole("button", { name: "Expand sidebar" })).toBeInTheDocument();
  });

  // Every other icon-only control in the shell carries a hover hint; this one is on
  // every page, and without it a mouse reader has nothing but the glyph to go on.
  it("says what its icon does on hover, in both states", async () => {
    const user = userEvent.setup();
    expect(screen.queryByRole("button", { name: "Collapse sidebar" })).toBeNull();
    renderShell();
    const collapse = screen.getByRole("button", { name: "Collapse sidebar" });
    expect(collapse).toHaveAttribute("title", "Collapse sidebar");

    await user.click(collapse);
    expect(screen.getByRole("button", { name: "Expand sidebar" })).toHaveAttribute(
      "title",
      "Expand sidebar",
    );
  });

  it("comes back open again once the reader opens it", async () => {
    const user = userEvent.setup();
    const first = renderShell();
    await user.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    await user.click(screen.getByRole("button", { name: "Expand sidebar" }));
    first.unmount();

    renderShell();
    expect(railCollapsed()).toBe(false);
  });

  it("renders open when the store refuses to answer", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => undefined,
    } as unknown as Storage);
    renderShell();
    // A store that will not answer costs the reader the memory of their rail
    // and nothing else — the shell still lays out.
    expect(railCollapsed()).toBe(false);
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  });
});

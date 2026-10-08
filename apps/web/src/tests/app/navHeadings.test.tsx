// The sidebar names its org and platform sections, but a person's own pages
// sit at the top with no heading over them.

import { MemoryRouter, Route, Routes } from "react-router-dom";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/auth", () => ({
  useCurrentUser: () => ({
    data: { display_name: "Vera Ng", email: "vera@x.io", is_platform_admin: false },
  }),
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

afterEach(cleanup);

describe("the sidebar's section headings", () => {
  it("shows no heading over a person's own pages, and keeps Organization's", () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/" element={<p>Home</p>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    const nav = screen.getAllByRole("navigation", { name: "Primary" })[0];
    expect(within(nav).getByRole("link", { name: /Chat/ })).toBeInTheDocument();
    expect(within(nav).queryByText("Personal")).toBeNull();
    expect(within(nav).getByText("Organization")).toBeInTheDocument();
  });
});

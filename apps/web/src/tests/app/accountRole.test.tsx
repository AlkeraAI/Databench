import { MemoryRouter, Route, Routes } from "react-router-dom";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// What the account chip says the reader IS.
//
// The role comes from one read. `is_org_admin ? "Org admin" : "Member"` would read an absent
// document as a negative and tell an org admin, during a failed read, that they are a member. A
// role nobody could establish is no answer yet, and the chip says nothing until there is one. React Query keeps the last successful document across a failing refetch, so a role once
// established survives an outage rather than flickering away.

const identity: { data?: { is_org_admin: boolean } } = { data: undefined };

vi.mock("@/api/auth", () => ({
  useCurrentUser: () => ({ data: { display_name: "Vera Ng", email: "vera@x.io" } }),
  useLogout: () => ({ mutate: vi.fn(), isPending: false }),
  useMemberships: () => ({ data: undefined }),
  useSwitchOrg: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/api/dashboard", () => ({
  useCurrentUser: () => ({ data: undefined }),
  useIdentityDashboard: () => identity,
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

afterEach(() => {
  cleanup();
  identity.data = undefined;
});

describe("the account chip's role line", () => {
  it("claims nothing while the read has produced no answer", () => {
    identity.data = undefined;
    renderShell();

    expect(screen.queryByText("Member")).toBeNull();
    expect(screen.queryByText("Org admin")).toBeNull();
    // The reader is still named — it is the ROLE that is unknown, not the account.
    expect(screen.getByText("Vera Ng")).toBeInTheDocument();
  });

  it.each([
    { admin: true, says: "Org admin", not: "Member" },
    { admin: false, says: "Member", not: "Org admin" },
  ])("says $says once the read answers", ({ admin, says, not }) => {
    identity.data = { is_org_admin: admin };
    renderShell();

    expect(screen.getByText(says)).toBeInTheDocument();
    expect(screen.queryByText(not)).toBeNull();
  });
});

// The users register's abuse-forensics columns: verification pill, disposable
// seal, signup date, month-to-date spend, signup IP — plus the sort toggle
// (newest ↔ top spend) and the widened search (IP included).

import { MemoryRouter } from "react-router-dom";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import shared from "@/pages/platform/admin/admin.module.css";
import { date, usd } from "@/pages/platform/admin/shared/format";

const FARM_USER = {
  id: "11111111-1111-1111-1111-111111111111",
  email: "burner@mailinator.com",
  display_name: "Burner Account",
  org_team_id: "aaaa1111-1111-1111-1111-111111111111",
  org_name: "Farm Org",
  platform_role: null,
  is_active: true,
  created_at: "2026-07-29T10:00:00Z",
  email_verified_at: null,
  disposable_email: true,
  signup_ip: "203.0.113.9",
  last_login_ip: "203.0.113.9",
  mtd_billed_nanos: 250_000_000_000,
  mtd_request_count: 41,
  banned: true,
  ban_reason: "Account farming",
};

const CLEAN_USER = {
  id: "22222222-2222-2222-2222-222222222222",
  email: "ada@example.com",
  display_name: "Ada Lovelace",
  org_team_id: "bbbb2222-2222-2222-2222-222222222222",
  org_name: "Northwind Labs",
  platform_role: null,
  is_active: true,
  created_at: "2026-07-30T09:00:00Z",
  email_verified_at: "2026-07-30T10:00:00Z",
  disposable_email: false,
  signup_ip: null,
  last_login_ip: null,
  mtd_billed_nanos: 0,
  mtd_request_count: 0,
  banned: false,
  ban_reason: null,
};

vi.mock("@/api/admin/admin", () => ({
  // Newest-first, as the backend serves it: CLEAN (Jul 30) then FARM (Jul 29).
  useAdminUsers: () => ({ data: [CLEAN_USER, FARM_USER], isError: false, refetch: vi.fn() }),
}));

const { AdminUsersPage } = await import("@/pages/platform/admin/users/AdminUsersPage");
const { TopbarSlotsContext } = await import("@/app/Topbar");

afterEach(cleanup);

const renderPage = () =>
  render(
    <MemoryRouter>
      <AdminUsersPage />
    </MemoryRouter>,
  );

const bodyRows = () => {
  const rows = screen.getAllByRole("row");
  return rows.filter((r) => within(r).queryAllByRole("cell").length > 0);
};

describe("AdminUsersPage", () => {
  it("subtitles the register without naming the threat model it serves", () => {
    // "the columns that expose account farming" put an internal threat-model term
    // in a page subtitle; the columns still do that, the subtitle need not say so.
    // The subtitle rides a topbar portal, so the test supplies the slot.
    const slot = document.createElement("div");
    document.body.append(slot);
    render(
      <MemoryRouter>
        <TopbarSlotsContext.Provider
          value={{ subtitle: slot, actions: null, framed: true, setTitleHidden: () => {}, setTopbarHidden: () => {} }}
        >
          <AdminUsersPage />
        </TopbarSlotsContext.Provider>
      </MemoryRouter>,
    );
    expect(within(slot).getByText("Accounts on the platform")).toBeInTheDocument();
    expect(within(slot).queryByText(/account farming/i)).not.toBeInTheDocument();
    slot.remove();
  });
});

describe("AdminUsersPage forensics columns", () => {
  it("renders verification state, signup date, MTD spend, IP, and the disposable seal", () => {
    renderPage();
    const farm = bodyRows()[1];
    expect(within(farm).getByText("Unverified")).toBeInTheDocument();
    expect(within(farm).getByText("Disposable")).toBeInTheDocument();
    expect(within(farm).getByText(date(FARM_USER.created_at))).toBeInTheDocument();
    expect(within(farm).getByText(usd(FARM_USER.mtd_billed_nanos))).toBeInTheDocument();
    expect(within(farm).getByText("203.0.113.9")).toBeInTheDocument();

    const clean = bodyRows()[0];
    expect(within(clean).getByText("Verified")).toBeInTheDocument();
    expect(within(clean).queryByText("Disposable")).toBeNull();
    // Zero spend renders as a dash-like placeholder, not "$0.00" noise.
    expect(within(clean).queryByText(usd(0))).toBeNull();
  });

  it("sorts by month-to-date spend when Top spend is picked", async () => {
    renderPage();
    expect(within(bodyRows()[0]).getByText("Ada Lovelace")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Top spend" }));
    expect(within(bodyRows()[0]).getByText("Burner Account")).toBeInTheDocument();
  });

  it("greys a banned row, keeps it clickable, and pills it with the reason", () => {
    renderPage();
    const banned = bodyRows()[1];
    expect(banned).toHaveClass(shared.bannedRow);
    // Greyed, never hidden or disabled: the row still links into the detail page.
    expect(within(banned).getByRole("link", { name: "Burner Account" })).toHaveAttribute(
      "href",
      `/admin/users/${FARM_USER.id}`,
    );
    expect(within(banned).getByText("Banned")).toBeInTheDocument();
    expect(within(banned).getByText("Banned").closest("[title]")).toHaveAttribute("title", "Account farming");

    const clean = bodyRows()[0];
    expect(clean).not.toHaveClass(shared.bannedRow);
    expect(within(clean).queryByText("Banned")).toBeNull();
  });

  it("finds accounts by signup IP through the search box", () => {
    renderPage();
    fireEvent.change(screen.getByRole("searchbox", { name: /search users/i }), {
      target: { value: "203.0.113" },
    });
    const rows = bodyRows();
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Burner Account")).toBeInTheDocument();
  });
});

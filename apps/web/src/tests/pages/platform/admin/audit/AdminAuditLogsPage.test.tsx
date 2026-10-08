import { MemoryRouter } from "react-router-dom";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { dateTime, dateTimeExact } from "@/pages/platform/admin/shared/format";

// The platform audit log with its api/admin/audit hook (the costly boundary) mocked; the page's
// rendering runs for real. The behavior pinned: the table's When column reads short (a scannable
// minute-resolution column) while the detail drawer's When field reads exact (seconds, to correlate
// against server logs) — the same split the org audit log keeps. Every expectation comes from the
// SAME shared formatters the page imports.

// Non-zero seconds, so the minute and second readings of the instant cannot agree.
const AT = "2026-06-01T12:00:43Z";

const ENTRY = {
  id: "0f0e0d0c-1111-2222-3333-444455556666",
  actor_id: "9a9b9c9d-1111-2222-3333-444455556666",
  actor_email: "root@example.com",
  actor_platform_role: "alkera_admin",
  action: "orgs.credit.grant",
  method: "POST",
  path: "/admin/v1/orgs/o1/credits",
  status_code: 200,
  target: null as string | null,
  detail: { amount_usd: "50" } as Record<string, unknown> | null,
  created_at: AT,
};

const DELETE = {
  ...ENTRY,
  id: "0f0e0d0c-1111-2222-3333-777777777777",
  action: "delete_org",
  method: "DELETE",
  path: "/admin/v1/orgs/7c1e",
  status_code: 204,
  target: "Acme Robotics",
  detail: { path_params: { org_id: "7c1e" } },
};

// The hook is the network boundary; each call records the filters the page asked with, and the
// fake server answers them the way the route does (an exact action match).
const { asked } = vi.hoisted(() => ({ asked: [] as Record<string, string | undefined>[] }));
vi.mock("@/api/admin/audit", () => ({
  useAuditLogs: (_page: number, _size: number, filters: Record<string, string | undefined> = {}) => {
    asked.push(filters);
    const items = [ENTRY, DELETE].filter((e) => !filters.action || e.action === filters.action);
    return {
      data: { items, total: items.length, page: 1, page_size: 50, actions: ["delete_org", "orgs.credit.grant", "set_platform_role"] },
      isError: false,
      refetch: vi.fn(),
    };
  },
}));

const { AdminAuditLogsPage } = await import("@/pages/platform/admin/audit/AdminAuditLogsPage");

afterEach(cleanup);

const renderPage = () =>
  render(
    <MemoryRouter>
      <AdminAuditLogsPage />
    </MemoryRouter>,
  );

/** The index of the When column, so the assertion pins the cell that column owns. */
const whenColumn = (): number =>
  screen.getAllByRole("columnheader").findIndex((h) => h.textContent === "When");

const eventRow = () =>
  screen.getByRole("row", { name: "View details for orgs.credit.grant" }) as HTMLTableRowElement;

const column = (name: string): number =>
  screen.getAllByRole("columnheader").findIndex((h) => h.textContent === name);

describe("AdminAuditLogsPage instants", () => {
  it("distinguishes the short and exact readings of the same instant", () => {
    // The floor the rest of this block stands on: if the two formatters agreed, every
    // pin below would pass no matter which one the page called.
    expect(dateTimeExact(AT)).not.toBe(dateTime(AT));
  });

  it("reads the table's When column short", () => {
    renderPage();
    // Exact equality, not a substring: in a 24-hour locale the short reading is a
    // prefix of the exact one, and a substring match would accept either.
    expect(eventRow().cells[whenColumn()].textContent).toBe(dateTime(AT));
    // The seconds-bearing reading belongs to the drawer, not the column.
    expect(screen.queryAllByText(dateTimeExact(AT))).toHaveLength(0);
  });

  it("reads the drawer's When field exact", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(eventRow());
    const panel = screen.getByRole("dialog");
    const field = within(panel).getByText("When");
    expect(field.parentElement?.querySelector("dd")?.textContent).toBe(dateTimeExact(AT));
  });
});

describe("AdminAuditLogsPage targets and filters", () => {
  it("names what each action was done to, and a dash when it names nothing", () => {
    renderPage();
    const del = screen.getByRole("row", { name: "View details for delete_org" }) as HTMLTableRowElement;
    expect(del.cells[column("Target")].textContent).toBe("Acme Robotics");
    expect(eventRow().cells[column("Target")].textContent).toBe("—");
  });

  it("opens the row's record with its target", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("row", { name: "View details for delete_org" }));
    const panel = screen.getByRole("dialog");
    expect(within(panel).getByText("Target").parentElement?.querySelector("dd")?.textContent).toBe("Acme Robotics");
  });

  it("filters by an action the log holds and clears back to everything", async () => {
    asked.length = 0;
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "Action" }));
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual([
      "All actions",
      "delete_org",
      "orgs.credit.grant",
      "set_platform_role",
    ]);
    await user.click(screen.getByRole("option", { name: "delete_org" }));
    expect(asked.at(-1)).toMatchObject({ action: "delete_org" });
    expect(screen.queryByRole("row", { name: "View details for orgs.credit.grant" })).toBeNull();
    expect(screen.getByRole("row", { name: "View details for delete_org" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(asked.at(-1)).toEqual({ action: undefined, actor_email: undefined, created_after: undefined, created_before: undefined });
    expect(eventRow()).toBeInTheDocument();
  });

  it("sends the actor filter only once it is committed", async () => {
    asked.length = 0;
    const user = userEvent.setup();
    renderPage();
    await user.type(screen.getByLabelText("Actor"), "root@example.com");
    expect(asked.some((f) => f.actor_email)).toBe(false);
    await user.keyboard("{Enter}");
    expect(asked.at(-1)).toMatchObject({ actor_email: "root@example.com" });
  });

  it("says no action matches when a filter empties the page, and keeps the filters", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "Action" }));
    await user.click(await screen.findByRole("option", { name: "set_platform_role" }));
    expect(screen.getByText("No matching actions")).toBeInTheDocument();
    expect(screen.queryByText("No actions recorded")).toBeNull();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });
});

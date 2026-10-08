import { MemoryRouter } from "react-router-dom";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { formatDateTime, formatDateTimeExact } from "@/lib/format/date";

// The org-admin audit log, with its api/orgAdminAudit hook (the costly boundary) mocked; the page's
// rendering + pagination run for real. The behavior pinned: the table shows only the core columns
// (When / Actor / Action / Target — no Detail column); clicking a row opens a side panel with the
// full payload; the empty state shows when there are none; the pager advances the offset and disables
// at the ends. The hooks record the requested offset + filters so we can prove Next actually
// re-queries the next page, and that the filter controls narrow the query rather than the rendering.

type Query<T> = {
  data?: T;
  isPending?: boolean;
  isError?: boolean;
  isFetching?: boolean;
  error?: Error;
  refetch?: () => void;
  mutate?: () => void;
};

type Verification = {
  ok: boolean;
  checked: number;
  broken_event_id: string | null;
  broken_at: string | null;
  head_hash: string | null;
};

const EVENT = (id: string) => ({
  id,
  actor_email: "ada@acme.com",
  action: "billing.pool.funded",
  target: "pool-1",
  detail: { amount_usd: "50" } as Record<string, unknown> | null,
  created_at: "2026-06-01T12:00:00Z",
});

// The instant a broken chain snapped. Deliberately a different moment from any event's `created_at`,
// with non-zero seconds, so a reading that came from `broken_at` can't be confused with the table's
// rendering of a row.
const BROKEN_AT = "2026-03-14T09:26:53Z";

let requestedOffset = 0;
let requestedFilters: Record<string, unknown> = {};
let csvFilters: Record<string, unknown> = {};
const h = {
  audit: {} as Query<{ events: ReturnType<typeof EVENT>[]; total: number; offset: number; limit: number }>,
  verify: {} as Query<Verification>,
};

vi.mock("@/api/orgAdminAudit", () => ({
  useOrgAudit: (offset: number, _limit: number, filters: Record<string, unknown>) => {
    requestedOffset = offset;
    requestedFilters = filters;
    return h.audit;
  },
  useAuditVerify: () => h.verify,
  auditCsvUrl: (filters: Record<string, unknown>) => {
    csvFilters = filters;
    return "https://api.example.com/api/v1/org/audit-events/export.csv";
  },
}));

// This suite exercises the real audit table, so the server gate must resolve to
// "enabled" (its gated branch is covered separately in featureGate.test).
vi.mock("@/api/dashboard", () => ({
  useIdentityDashboard: () => ({ data: { enterprise_features_enabled: true }, isPending: false }),
}));

const { AuditLogPage, PAGE } = await import("@/pages/organization/org/AuditLogPage");
const { TopbarSlotsContext } = await import("@/app/Topbar");

afterEach(() => {
  cleanup();
  document.getElementById("test-topbar-actions")?.remove();
});
beforeEach(() => {
  requestedOffset = 0;
  requestedFilters = {};
  csvFilters = {};
  // 120 total over a PAGE-sized window — two full pages plus a remainder, so Next/Previous both engage.
  h.audit = { data: { events: [EVENT("e1"), EVENT("e2")], total: 120, offset: 0, limit: PAGE }, refetch: vi.fn() };
  h.verify = { data: undefined, isPending: false, mutate: vi.fn() };
});

// The page's topbar buttons portal into the shell's masthead slot; the test stands the slot up
// so Verify chain / Export CSV render somewhere queryable.
const renderPage = () => {
  const actions = document.createElement("div");
  actions.id = "test-topbar-actions";
  document.body.appendChild(actions);
  return render(
    <MemoryRouter>
      <TopbarSlotsContext.Provider value={{ subtitle: null, actions, framed: true, setTitleHidden: () => {}, setTopbarHidden: () => {} }}>
        <AuditLogPage />
      </TopbarSlotsContext.Provider>
    </MemoryRouter>,
  );
};

/** Pick an action family the way a user does: open the listbox, click the row. */
const pickAction = async (user: ReturnType<typeof userEvent.setup>, label: string) => {
  await user.click(screen.getByRole("button", { name: "Action" }));
  await user.click(await screen.findByRole("option", { name: label }));
};

describe("AuditLogPage", () => {
  it("renders events with actor, action, and target", () => {
    renderPage();
    expect(screen.getAllByText("ada@acme.com")).toHaveLength(2);
    expect(screen.getAllByText("billing.pool.funded")).toHaveLength(2);
    expect(screen.getAllByText("pool-1")).toHaveLength(2);
  });

  it("shows the range against the total", () => {
    renderPage();
    expect(screen.getByText(new RegExp(`1–${PAGE} of 120`))).toBeInTheDocument();
  });

  it("carries no Detail column — the payload lives in the panel", () => {
    renderPage();
    expect(screen.queryByRole("columnheader", { name: /detail/i })).not.toBeInTheDocument();
  });

  it("opens a side panel with the full event detail when a row is clicked", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getAllByRole("row", { name: /view details for billing.pool.funded/i })[0]);

    const panel = screen.getByRole("dialog");
    // The core-field ledger, plus the detail dumped whole as a catch-all (raw key + value), not
    // split into promoted fields.
    expect(within(panel).getByText("billing.pool.funded")).toBeInTheDocument();
    expect(within(panel).getByText("ada@acme.com")).toBeInTheDocument();
    expect(panel).toHaveTextContent("amount_usd");
    expect(panel).toHaveTextContent("50");
  });

  it("opens the panel from the keyboard (Enter on a focused row)", () => {
    renderPage();
    const row = screen.getAllByRole("row", { name: /view details for/i })[0];
    fireEvent.keyDown(row, { key: "Enter" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("shows only the summary when an event carries no detail payload", async () => {
    const bare = { ...EVENT("e1"), detail: null };
    h.audit = { data: { events: [bare], total: 1, offset: 0, limit: PAGE }, refetch: vi.fn() };
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("row", { name: /view details for/i }));
    const panel = screen.getByRole("dialog");
    // The core fields still read; with no extra detail there is simply no JSON block, not a placeholder.
    expect(within(panel).getByText("ada@acme.com")).toBeInTheDocument();
    expect(panel).not.toHaveTextContent("amount_usd");
  });

  it("renders the empty state when there are no events", () => {
    h.audit = { data: { events: [], total: 0, offset: 0, limit: PAGE }, refetch: vi.fn() };
    renderPage();
    expect(screen.getByText(/no events yet/i)).toBeInTheDocument();
  });

  it("Previous is disabled on the first page; Next advances the offset", async () => {
    const user = userEvent.setup();
    renderPage();
    expect(screen.getByRole("button", { name: /previous/i })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /next/i }));
    expect(requestedOffset).toBe(PAGE);
  });

  it("Next is disabled on the last page", async () => {
    const user = userEvent.setup();
    // Two pages (PAGE+1 over a PAGE window); step to the last, where Next can advance no further.
    h.audit = { data: { events: [EVENT("e1")], total: PAGE + 1, offset: 0, limit: PAGE }, refetch: vi.fn() };
    renderPage();
    await user.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();
  });
});

describe("chain verification", () => {
  it("runs the check only when the topbar button is clicked", async () => {
    const user = userEvent.setup();
    renderPage();
    expect(h.verify.mutate).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /verify chain/i }));
    expect(h.verify.mutate).toHaveBeenCalledTimes(1);
  });

  it("is disabled while the check runs", () => {
    h.verify = { data: undefined, isPending: true, mutate: vi.fn() };
    renderPage();
    expect(screen.getByRole("button", { name: /verifying/i })).toBeDisabled();
  });

  it("announces an intact chain with the count and a copyable head hash", () => {
    const hash = "a".repeat(32) + "b".repeat(32);
    h.verify = {
      data: { ok: true, checked: 1204, broken_event_id: null, broken_at: null, head_hash: hash },
      mutate: vi.fn(),
    };
    renderPage();
    expect(screen.getByText(/chain intact/i)).toBeInTheDocument();
    expect(screen.getByText(/1,204 events in the chain verify/)).toBeInTheDocument();
    // The list also shows staff actions the chain does not hold; the count must not read as the list's.
    expect(screen.getByText(/recorded outside the chain/)).toBeInTheDocument();
    // The hash shows truncated but the copy control carries the full value.
    expect(screen.getByText(new RegExp(hash.slice(0, 16)))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
  });

  it("surfaces the first broken row with its id and timestamp", () => {
    h.verify = {
      data: {
        ok: false,
        checked: 42,
        broken_event_id: "5c0ffee5-0000-0000-0000-000000000042",
        broken_at: BROKEN_AT,
        head_hash: null,
      },
      mutate: vi.fn(),
    };
    const { container } = renderPage();
    expect(screen.getByText(/chain broken/i)).toBeInTheDocument();
    expect(screen.getByText(/5c0ffee5-0000-0000-0000-000000000042/)).toBeInTheDocument();
    expect(screen.getByText(/42 events before it/)).toBeInTheDocument();
    // The timestamp this test's name has always promised: the seconds-bearing reading, since an
    // operator correlates this row against server logs. It sits inside a sentence, so match on the
    // surface's text rather than a single node.
    expect(container).toHaveTextContent(formatDateTimeExact(BROKEN_AT));
  });

  it("reports a failed verification without claiming a result", () => {
    h.verify = { data: undefined, isError: true, error: new Error("could not verify the audit chain"), mutate: vi.fn() };
    renderPage();
    expect(screen.getByText(/verification failed/i)).toBeInTheDocument();
    expect(screen.queryByText(/chain intact/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/chain broken/i)).not.toBeInTheDocument();
  });
});

describe("filters", () => {
  it("narrows the query to an action family and resets to the first page", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: /next/i }));
    expect(requestedOffset).toBe(PAGE);
    await pickAction(user, "Agent activity");
    expect(requestedFilters.action).toBe("agent.");
    expect(requestedOffset).toBe(0);
  });

  // Sign-in methods and knowledge sync are audited as `org_settings.*`; the
  // filter must be able to find them.
  it("filters to the organization settings changes", async () => {
    const user = userEvent.setup();
    renderPage();
    await pickAction(user, "Organization settings");
    expect(requestedFilters.action).toBe("org_settings.");
  });

  it("commits the actor email on Enter, not per keystroke", async () => {
    const user = userEvent.setup();
    renderPage();
    const input = screen.getByLabelText("Actor");
    await user.type(input, "ada@acme.com");
    expect(requestedFilters.actor_email).toBeUndefined();
    await user.type(input, "{Enter}");
    expect(requestedFilters.actor_email).toBe("ada@acme.com");
  });

  it("widens a picked day to its full local range", async () => {
    const user = userEvent.setup();
    renderPage();
    const today = new Date();
    const dayLabel = new Intl.DateTimeFormat("en-US", {
      month: "long",
      day: "numeric",
      year: "numeric",
    }).format(today);
    await user.click(screen.getByRole("button", { name: "From" }));
    await user.click(await screen.findByRole("button", { name: dayLabel }));
    await user.click(screen.getByRole("button", { name: "To" }));
    await user.click(await screen.findByRole("button", { name: dayLabel }));
    // Expected bounds derived through the numeric Date constructor — independent of the page's
    // string-parse formula, so a UTC-parse regression there fails here.
    const [y, m, d] = [today.getFullYear(), today.getMonth(), today.getDate()];
    expect(requestedFilters.created_after).toBe(new Date(y, m, d, 0, 0, 0, 0).toISOString());
    expect(requestedFilters.created_before).toBe(new Date(y, m, d, 23, 59, 59, 999).toISOString());
  });

  it("says a backwards range is backwards instead of implying the log is empty there", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 8, 20, 12));
    try {
      const user = userEvent.setup();
      renderPage();
      await user.click(screen.getByRole("button", { name: "From" }));
      await user.click(await screen.findByRole("button", { name: "September 20, 2026" }));
      // The server matches nothing for a backwards range.
      h.audit = { data: { events: [], total: 0, offset: 0, limit: PAGE }, refetch: vi.fn() };
      await user.click(screen.getByRole("button", { name: "To" }));
      await user.click(await screen.findByRole("button", { name: "September 2, 2026" }));
      expect(await screen.findByText("The From date is after the To date.")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("dates come from the custom picker — the browser's native control never renders", () => {
    renderPage();
    expect(document.querySelector('input[type="date"]')).toBeNull();
  });

  it("every action family carries its mark in the listbox", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "Action" }));
    const options = await screen.findAllByRole("option");
    const [all, ...families] = options;
    expect(all).toHaveTextContent("All actions");
    for (const family of families) expect(family.querySelector("svg")).not.toBeNull();
  });

  it("hands the active filters to the CSV export link", async () => {
    const user = userEvent.setup();
    renderPage();
    await pickAction(user, "Agent activity");
    expect(csvFilters.action).toBe("agent.");
  });

  it("shows a no-match state with a working Clear filters, never 'No events yet'", async () => {
    const user = userEvent.setup();
    renderPage();
    // The filter comes back empty: the re-render triggered by picking it reads the empty page.
    h.audit = { data: { events: [], total: 0, offset: 0, limit: PAGE }, refetch: vi.fn() };
    await pickAction(user, "Agent activity");
    expect(screen.getByText(/no matching events/i)).toBeInTheDocument();
    expect(screen.queryByText(/no events yet/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /clear filters/i }));
    expect(requestedFilters).toEqual({});
    // With nothing filtered and nothing stored, the truly-empty reading returns.
    expect(screen.getByText(/no events yet/i)).toBeInTheDocument();
  });
});

// Timestamps go through the portal's shared date policy — the viewer's own zone and locale — so every
// expectation here is produced by the SAME formatters the page imports. The table reads short (a
// scannable column) and the forensic surfaces read exact (seconds, to correlate against server logs);
// an audit row is worthless for that if the seconds are dropped.
describe("instants", () => {
  const AT = EVENT("e1").created_at;
  // The label of the panel's first summary row and of the table's first column.
  const WHEN = "When";

  /** The index of the When column, so the assertion pins the cell that column owns. */
  const whenColumn = (): number => screen.getAllByRole("columnheader").findIndex((h) => h.textContent === WHEN);

  const firstEventRow = () => screen.getAllByRole("row", { name: /view details for/i })[0] as HTMLTableRowElement;

  it("distinguishes the short and exact readings of the same instant", () => {
    // The floor the rest of this block stands on: if the two formatters agreed, every pin below
    // would pass no matter which one the page called.
    expect(formatDateTimeExact(AT)).not.toBe(formatDateTime(AT));
  });

  it("reads the table's When column short", () => {
    renderPage();
    // Exact equality, not a substring: in a 24-hour locale the short reading is a prefix of the
    // exact one, and a substring match would accept either.
    expect(firstEventRow().cells[whenColumn()].textContent).toBe(formatDateTime(AT));
    // The seconds-bearing reading belongs to the drawer, not the column.
    expect(screen.queryAllByText(formatDateTimeExact(AT))).toHaveLength(0);
  });

  it("reads the drawer's When field exact", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(firstEventRow());
    const panel = screen.getByRole("dialog");
    const field = within(panel).getByText(WHEN);
    expect(field.parentElement?.querySelector("dd")?.textContent).toBe(formatDateTimeExact(AT));
  });

  it("claims no instant when the chain verifies", () => {
    // `broken_at` is null on an intact chain: the callout must state nothing rather than render a
    // fallback reading of the epoch.
    h.verify = {
      data: { ok: true, checked: 7, broken_event_id: null, broken_at: null, head_hash: "f".repeat(64) },
      mutate: vi.fn(),
    };
    const { container } = renderPage();
    expect(container).not.toHaveTextContent(formatDateTimeExact(new Date(0).toISOString()));
  });
});

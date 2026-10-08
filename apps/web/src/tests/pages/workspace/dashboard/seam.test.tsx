import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";

import { TopbarSlotsContext } from "@/app/Topbar";
import type { DashboardData, DashboardState } from "@/pages/workspace/dashboard/model";

// The dashboard seam: the page reads useDashboardData(), which resolves to a DashboardState. Two
// things are under test here, both via observable output (rendered surfaces / classified status),
// never class names:
//   1. An INJECTED provider (preview/test) drives the page directly — each state must render its
//      own first-class surface, and the injected state must win over the live path.
//   2. The LIVE classifier (useLiveDashboardState) maps every React Query result to a status: any
//      one error → error; any one not-yet-resolved → loading; all resolved → ready.
// The hooks are mocked because they are the costly external boundary (network); everything
// else — the classifier, the memo, the rendered states — runs for real.

// Mocked per-test; each entry is the object a useQuery hook returns (only the fields the classifier
// reads). Reset before each test so cases don't leak.
type FakeQuery = { data?: unknown; isError?: boolean; error?: unknown; refetch?: () => unknown };
const hooks: Record<string, FakeQuery> = {
  identity: { isError: false },
  connections: { isError: false },
  chats: { isError: false },
};

vi.mock("@/api/dashboard", () => ({ useIdentityDashboard: () => hooks.identity }));
vi.mock("@/api/connections", () => ({ useMyConnections: () => hooks.connections }));
vi.mock("@/api/chats", () => ({ useChats: () => hooks.chats }));

// Import AFTER the mock is registered.
const { DashboardDataProvider, LiveDashboardProvider, useDashboardData, resetDashboardStore } = await import(
  "@/pages/workspace/dashboard/provider"
);
const { DashboardPage } = await import("@/pages/workspace/dashboard/DashboardPage");

afterEach(cleanup);
beforeEach(() => {
  resetDashboardStore();
  for (const name of QUERIES) hooks[name] = { isError: false, refetch: () => {} };
});

// A probe that surfaces the resolved status + message for assertions, without the full page.
function StatusProbe() {
  const { status, errorMessage } = useDashboardData();
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="message">{errorMessage ?? ""}</span>
    </div>
  );
}

// Mount real subtitle/actions slot nodes (AppLayout owns these in the app) so the page's masthead
// portal has somewhere to render — without them TopbarSubtitle/Actions return null and the greeting
// never reaches the DOM. A MemoryRouter satisfies the page's <Link>s and SearchBar.
function PageHarness({ state }: { state: DashboardState }) {
  const [subtitle, setSubtitle] = useState<HTMLElement | null>(null);
  const [actions, setActions] = useState<HTMLElement | null>(null);
  return (
    <MemoryRouter>
      <div ref={setSubtitle} data-slot="subtitle" />
      <div ref={setActions} data-slot="actions" />
      <TopbarSlotsContext.Provider value={{ subtitle, actions, framed: true, setTitleHidden: () => {}, setTopbarHidden: () => {} }}>
        <DashboardDataProvider value={state}>
          <DashboardPage />
        </DashboardDataProvider>
      </TopbarSlotsContext.Provider>
    </MemoryRouter>
  );
}

function seededData(): DashboardData {
  return {
    greetingName: "Jordan",
    orgName: "Tideline Analytics",
    stats: [
      {
        key: "plan",
        label: "Remaining",
        value: "63%",
        note: "37% of cycle used · resets in 12 d",
        to: "/settings/billing",
        viz: { kind: "ring", pct: 0.37, tip: "37% of the cycle used" },
      },
    ],
    chats: [{ id: "ch1", title: "Churn cohort", updated: "2h ago" }],
    panels: [],
  };
}

// --- injected-provider path ---------------------------------------------------------------------

describe("DashboardDataProvider — injected state drives the page", () => {
  it("keeps the raw cause under Details, out of the headline", () => {
    const retry = vi.fn();
    const rawMessage = "could not load your usage (503)";
    const state: DashboardState = { status: "error", data: null, errorMessage: rawMessage, retry };
    render(<PageHarness state={state} />);

    const alert = screen.getByRole("alert");
    expect(alert).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();

    // The heading is the focal point; the body is plain, non-empty, and is NOT the raw error.
    const heading = within(alert).getByRole("heading");
    expect(heading.textContent?.trim().length ?? 0).toBeGreaterThan(0);
    expect(heading.textContent).not.toContain(rawMessage);

    // The raw technical message is present (support can recover it) but lives INSIDE the
    // Details disclosure, never as the headline or the lead body line.
    const rawNode = within(alert).getByText(rawMessage);
    const details = alert.querySelector("details");
    expect(details).not.toBeNull();
    expect(details).toContainElement(rawNode);
    expect(heading).not.toContainElement(rawNode);
    expect(within(details as HTMLElement).getByText(/details/i)).toBeInTheDocument();
  });

  it("omits the Details disclosure when there is no raw error message", () => {
    const state: DashboardState = { status: "error", data: null, errorMessage: null, retry: () => {} };
    render(<PageHarness state={state} />);
    const alert = screen.getByRole("alert");
    // A plain body still tells the user what to do, but with no message there's nothing to disclose.
    expect(alert.querySelector("details")).toBeNull();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("renders the page grid, with no greeting line, when status is ready", () => {
    const state: DashboardState = { status: "ready", data: seededData(), errorMessage: null, retry: () => {} };
    render(<PageHarness state={state} />);
    expect(screen.getByText("Chats")).toBeInTheDocument();
    expect(screen.getByText("63%")).toBeInTheDocument();
    // No greeting line: the overview topbar names the page only.
    expect(screen.queryByText(/welcome back/i)).toBeNull();
  });
});

// --- live classifier path (no injected provider → falls to live, hooks mocked) ------------------

const raw: Record<string, unknown> = {
  identity: { user: { first_name: "Jordan", display_name: "Jordan" }, org: { name: "Tideline" } },
  connections: [],
  chats: { items: [] },
};

/** Every query the live classifier waits on. */
const QUERIES = ["identity", "connections", "chats"] as const;

/** Seed every query with a resolved response — the all-clear the classifier reads as ready. */
function resolveAll(): void {
  for (const name of QUERIES) hooks[name].data = raw[name];
}

describe("useLiveDashboardState — classifies every query result", () => {
  // A seat with nothing recorded yet still resolves to the working page: the cards read zero and
  // the chats panel's start action is the first step, so there is no dead-end empty branch.
  it("classifies a fully-resolved, entirely-empty seat as ready", () => {
    resolveAll();
    render(
      <LiveDashboardProvider>
        <StatusProbe />
      </LiveDashboardProvider>,
    );
    expect(screen.getByTestId("status").textContent).toBe("ready");
  });

  // One undefined data → still loading (the page must not flash a half-built grid mid-fetch).
  it.each(QUERIES)("classifies as loading while %s has not resolved", (pending) => {
    resolveAll();
    hooks[pending].data = undefined;
    render(
      <LiveDashboardProvider>
        <StatusProbe />
      </LiveDashboardProvider>,
    );
    expect(screen.getByTestId("status").textContent).toBe("loading");
  });

  // A single failed query (partial failure) classifies the whole dashboard as error, and the
  // message comes from the query that ACTUALLY errored — not the first query in the list (fix 4).
  it.each(QUERIES)(
    "classifies as error when %s fails, surfacing that query's message",
    (failing) => {
      resolveAll();
      hooks[failing] = { isError: true, error: new ApiError(403, { error: { code: "forbidden", message: `boom from ${failing}` } }), refetch: () => {} };
      render(
      <LiveDashboardProvider>
        <StatusProbe />
      </LiveDashboardProvider>,
    );
      expect(screen.getByTestId("status").textContent).toBe("error");
      expect(screen.getByTestId("message").textContent).toBe(`boom from ${failing}`);
    },
  );

  // Fix 4 specifically: when an EARLIER query has only data (no error) and a LATER query is the one
  // that errored, the message must still come from the errored one. The pre-fix `find(q => q.error)`
  // could pick a non-errored query whose `.error` happens to be set; `find(q => q.isError)` cannot.
  it("prefers the erroring query's message over a stale error field", () => {
    // identity resolved with data AND a stale leftover error object, but isError false (not erroring).
    resolveAll();
    hooks.identity = { isError: false, data: raw.identity, error: new ApiError(403, { error: { code: "forbidden", message: "stale, not active" } }), refetch: () => {} };
    hooks.chats = { isError: true, error: new ApiError(403, { error: { code: "forbidden", message: "the real failure" } }), refetch: () => {} };
    render(
      <LiveDashboardProvider>
        <StatusProbe />
      </LiveDashboardProvider>,
    );
    expect(screen.getByTestId("status").textContent).toBe("error");
    expect(screen.getByTestId("message").textContent).toBe("the real failure");
  });

  // A non-Error rejection (a bare string thrown) must NOT leak the raw value into the UI — the seam
  // surfaces a non-empty generic fallback instead. We pin the contract (errors → a safe, non-empty
  // message that is not the raw thrown value), not the exact fallback prose, so harmless copy edits
  // to the fallback string don't break this test.
  it("falls back to a safe generic message for a non-Error", () => {
    const rawThrown = "a bare string";
    resolveAll();
    hooks.chats = { isError: true, error: rawThrown, refetch: () => {} };
    render(
      <LiveDashboardProvider>
        <StatusProbe />
      </LiveDashboardProvider>,
    );
    expect(screen.getByTestId("status").textContent).toBe("error");
    const message = screen.getByTestId("message").textContent ?? "";
    expect(message.length).toBeGreaterThan(0);
    expect(message).not.toBe(rawThrown);
  });
});

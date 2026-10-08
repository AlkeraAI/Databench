// An installed overview source: its cards land where it placed them, its panels follow the
// chats, and its reads join the overview's own loading and error states. The open reads are
// mocked (the network boundary); the point, the provider and the page run for real.

import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { OVERVIEW_SOURCES, type OverviewQuery, type OverviewSlice } from "@/app/extensions/portal";
import { TopbarSlotsContext } from "@/app/Topbar";

type FakeQuery = { data?: unknown; isError: boolean; error?: unknown; refetch: () => unknown };
const open: Record<"identity" | "connections" | "chats", FakeQuery> = {
  identity: { isError: false, refetch: () => {} },
  connections: { isError: false, refetch: () => {} },
  chats: { isError: false, refetch: () => {} },
};
let source: { queries: OverviewQuery[]; slice: OverviewSlice | null } = { queries: [], slice: null };

vi.mock("@/api/dashboard", () => ({ useIdentityDashboard: () => open.identity }));
vi.mock("@/api/connections", () => ({ useMyConnections: () => open.connections }));
vi.mock("@/api/chats", () => ({ useChats: () => open.chats }));

OVERVIEW_SOURCES.register({
  key: "test",
  placeholders: { stats: 1, panels: 1 },
  useSlice: () => source,
});

const { LiveDashboardProvider, resetDashboardStore } = await import("@/pages/workspace/dashboard/provider");
const { DashboardPage } = await import("@/pages/workspace/dashboard/DashboardPage");

const SLICE: OverviewSlice = {
  stats: [
    { stat: { key: "first", label: "First", value: "1", to: "/a" }, after: null },
    { stat: { key: "after-connections", label: "Later", value: "2", to: "/b" }, after: "connections" },
  ],
  panels: [{ key: "panel", node: <section aria-label="Contributed panel" /> }],
};

function Page() {
  const [actions, setActions] = useState<HTMLElement | null>(null);
  return (
    <MemoryRouter>
      <div ref={setActions} />
      <TopbarSlotsContext.Provider
        value={{ subtitle: null, actions, framed: true, setTitleHidden: () => {}, setTopbarHidden: () => {} }}
      >
        <LiveDashboardProvider>
          <DashboardPage />
        </LiveDashboardProvider>
      </TopbarSlotsContext.Provider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  resetDashboardStore();
  open.identity = { isError: false, refetch: () => {}, data: { user: { first_name: "Jo" }, org: { name: "Acme" } } };
  open.connections = { isError: false, refetch: () => {}, data: [] };
  open.chats = { isError: false, refetch: () => {}, data: { items: [] } };
  source = { queries: [], slice: SLICE };
});
afterEach(cleanup);

describe("an installed overview source", () => {
  it("places its cards by their anchors and its panels after the chats", () => {
    render(<Page />);
    const cards = screen.getAllByRole("link").filter((a) => /: \d+$/.test(a.getAttribute("aria-label") ?? ""));
    expect(cards.map((a) => a.getAttribute("aria-label"))).toEqual(["First: 1", "Connections: 0", "Later: 2"]);
    expect(screen.getByRole("region", { name: "Contributed panel" })).toBeInTheDocument();
  });

  it("holds the page in loading until its slice is ready", () => {
    source = { queries: [], slice: null };
    render(<Page />);
    expect(screen.getByLabelText("Loading dashboard")).toBeInTheDocument();
    expect(screen.queryByText("Connections")).toBeNull();
  });

  it("fails the page with its own read's message when that read fails", () => {
    source = {
      queries: [{ isError: true, error: new ApiError(403, { error: { code: "forbidden", message: "the source failed" } }), refetch: () => {} }],
      slice: null,
    };
    render(<Page />);
    expect(screen.getByRole("alert")).toHaveTextContent("the source failed");
  });
});

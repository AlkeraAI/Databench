// The ops page renders the summary it is served — the stat strip, the machines-per-state strip, the
// machine table, the health checks and the build — says so when there are no machines, and re-reads
// on the 30 s interval.

import { act, cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OPS_REFRESH_MS, type OpsSummary } from "@/api/admin/admin";
import { createQueryClient } from "@/api/queryClient";
import { AdminOpsPage, OPS_LABELS, stateTone } from "@/pages/platform/admin/ops/AdminOpsPage";
import { usd } from "@/pages/platform/admin/shared/format";

const SUMMARY: OpsSummary = {
  machines: [
    { id: "m-1", name: "tideline-box", org_id: "o-1", provider: "ec2", tenancy: "dedicated", state: "ready", chats_served: 3 },
    { id: "m-2", name: "pool-2", org_id: "o-2", provider: "runpod", tenancy: "pool", state: "unreachable", chats_served: 0 },
  ],
  machines_by_state: [
    { key: "ready", count: 1 },
    { key: "unreachable", count: 1 },
  ],
  machines_by_provider: [
    { key: "ec2", count: 1 },
    { key: "runpod", count: 1 },
  ],
  chats_served_total: 3,
  spend: { today_nanos: 1_500_000_000, month_to_date_nanos: 40_000_000_000, monthly_cap_nanos: 5_000_000_000_000, cap_headroom_nanos: 4_960_000_000_000 },
  crash_reports_24h: 2,
  crash_reports_unread: 5,
  health: {
    overall: "warn",
    checks: [
      { key: "postgres", label: "Postgres", status: "ok", detail: "reachable" },
      { key: "smtp", label: "SMTP", status: "warn", detail: "slow handshake" },
    ],
  },
  build_id: "build-7f3a",
  app_version: "1.4.0",
};

function serve(bodies: OpsSummary[]) {
  let served = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL((input as Request).url).pathname;
    if (path !== "/admin/v1/ops/summary") return new Response("{}", { status: 404 });
    const body = bodies[Math.min(served, bodies.length - 1)];
    served += 1;
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return () => served;
}

function renderPage() {
  const qc = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AdminOpsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("AdminOpsPage", () => {
  it("renders the summary it is served", async () => {
    serve([SUMMARY]);
    renderPage();
    expect(await screen.findByText(OPS_LABELS.chats)).toBeInTheDocument();
    expect(screen.getByText(usd(SUMMARY.spend.today_nanos))).toBeInTheDocument();
    expect(screen.getByText(usd(SUMMARY.spend.cap_headroom_nanos))).toBeInTheDocument();
    // No crash-report viewer is installed, so there is nothing to link the unread count to.
    expect(screen.getByText(OPS_LABELS.crashes)).toBeInTheDocument();
    expect(screen.queryByText(/unread/)).toBeNull();

    const strip = screen.getByLabelText(OPS_LABELS.byState);
    expect(within(strip).getByText("ready 1")).toBeInTheDocument();
    expect(within(strip).getByText("unreachable 1")).toBeInTheDocument();
    expect(screen.getByText("ec2 1 · runpod 1")).toBeInTheDocument();

    const box = screen.getByRole("link", { name: "tideline-box" });
    expect(box).toHaveAttribute("href", "/admin/orgs/o-1?tab=activity");
    expect(screen.getByText("slow handshake")).toBeInTheDocument();
    expect(screen.getByText("build-7f3a")).toBeInTheDocument();
  });

  it("says so in one sentence when there are no machines", async () => {
    serve([{ ...SUMMARY, machines: [], machines_by_state: [], machines_by_provider: [], chats_served_total: 0 }]);
    renderPage();
    expect(await screen.findAllByText(OPS_LABELS.machinesEmpty)).toHaveLength(2);
  });

  it("re-reads every 30 seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const served = serve([SUMMARY, { ...SUMMARY, chats_served_total: 9 }]);
    renderPage();
    await screen.findByText(OPS_LABELS.chats);
    expect(served()).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(OPS_REFRESH_MS);
    });
    expect(served()).toBe(2);
    expect(await screen.findByText("9")).toBeInTheDocument();
  });

  it.each([
    ["ready", "success"],
    ["ok", "success"],
    ["starting", "warning"],
    ["draining", "warning"],
    ["warn", "warning"],
    ["unreachable", "danger"],
    ["fail", "danger"],
    ["none", "neutral"],
    ["skipped", "neutral"],
  ])("reads %s as %s", (state, tone) => {
    expect(stateTone(state)).toBe(tone);
  });

  it("links the unread crash reports to the viewer when one is installed", async () => {
    // A fresh module graph, so the route point is not yet frozen by the cases above.
    vi.resetModules();
    const portal = await import("@/app/extensions/portal");
    portal.PORTAL_ROUTES.register({
      key: "admin.crash_reports",
      mount: "platformStaff",
      path: "/admin/crash-reports",
      element: <p>viewer</p>,
    });
    const fresh = await import("@/pages/platform/admin/ops/AdminOpsPage");
    serve([SUMMARY]);
    const qc = createQueryClient({ retry: false });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <fresh.AdminOpsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("link", { name: "5 unread" })).toHaveAttribute("href", "/admin/crash-reports");
  });
});

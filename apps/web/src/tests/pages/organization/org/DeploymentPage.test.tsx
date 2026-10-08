import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@alkera/sdk";

import { DeploymentPage } from "@/pages/organization/org/DeploymentPage";
import { filterNav, NAV, type NavGates } from "@/app/nav";

type Report = components["schemas"]["DeploymentHealthReport"];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const REPORT: Report = {
  overall: "warn",
  mode: "direct",
  backend_version: "1.2.3",
  last_run_at: new Date().toISOString(),
  last_trigger: "scheduled",
  last_duration_ms: 120,
  checks: [
    { key: "postgres", label: "Postgres", status: "ok", detail: "reachable", latency_ms: 3, org_scoped: false },
    { key: "temporal", label: "Task orchestrator", status: "ok", detail: "namespace 'default' reachable", latency_ms: 4, org_scoped: false },
    { key: "worker_beat", label: "Background worker & scheduler", status: "warn", detail: "starting", latency_ms: 0, org_scoped: false },
    { key: "provider_anthropic", label: "Anthropic API key", status: "fail", detail: "invalid key", latency_ms: 5, org_scoped: true },
  ],
};

type Call = { method: string; url: string };

function stubFetch(opts: { entitled: boolean }): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = input instanceof Request ? input.url : String(input);
      const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
      calls.push({ method, url });
      const json = (body: unknown, s = 200) =>
        new Response(JSON.stringify(body), { status: s, headers: { "content-type": "application/json" } });
      if (url.includes("/deployment-health/run")) return json({ ...REPORT, last_trigger: "manual" });
      if (url.includes("/deployment-health")) return json(REPORT);
      if (url.includes("/dashboard")) {
        return json({
          user: { first_name: "A", display_name: "Admin" },
          org: { name: "Org" },
          teams: [],
          pending_invitations: [],
          is_org_admin: true,
          entitled_features: opts.entitled ? ["byok"] : [],
        });
      }
      return json({});
    }),
  );
  return calls;
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return render(<DeploymentPage />, { wrapper });
}

describe("DeploymentPage", () => {
  it("renders the health banner + grouped checks with status pills", async () => {
    stubFetch({ entitled: false });
    renderPage();
    expect(await screen.findByText("Needs attention")).toBeInTheDocument(); // overall=warn
    expect(screen.getByText("Postgres")).toBeInTheDocument();
    expect(screen.getByText("Infrastructure")).toBeInTheDocument();
    // The org-scoped provider row carries an "org" badge.
    expect(screen.getByText("org")).toBeInTheDocument();
  });

  it("files the task orchestrator check under Infrastructure", async () => {
    stubFetch({ entitled: false });
    renderPage();
    const infrastructure = (await screen.findByText("Infrastructure")).closest(".alk-card");
    const connectivity = screen.getByText("Connectivity").closest(".alk-card");
    expect(infrastructure).not.toBeNull();
    expect(connectivity).not.toBeNull();
    expect(within(infrastructure as HTMLElement).getByText("Task orchestrator")).toBeInTheDocument();
    expect(within(infrastructure as HTMLElement).getByText("namespace 'default' reachable")).toBeInTheDocument();
    expect(within(connectivity as HTMLElement).queryByText("Task orchestrator")).toBeNull();
  });

  it("hides the Model providers tab when BYOK is not entitled", async () => {
    stubFetch({ entitled: false });
    renderPage();
    await screen.findByText("Health");
    expect(screen.queryByText("Model providers")).not.toBeInTheDocument();
  });

  it("shows the Model providers tab when entitled", async () => {
    stubFetch({ entitled: true });
    renderPage();
    expect(await screen.findByText("Model providers")).toBeInTheDocument();
  });

  it("runs the checks on Run now", async () => {
    const calls = stubFetch({ entitled: false });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /run now/i }));
    await waitFor(() => {
      expect(calls.some((c) => c.method === "POST" && c.url.includes("/deployment-health/run"))).toBe(true);
    });
  });
});

describe("nav self-hosted gate", () => {
  const gates = (over: Partial<NavGates>): NavGates => ({
    orgAdmin: true,
    teamAdmin: true,
    platformStaff: false,
    platformAdmin: false,
    selfHosted: false,
    ...over,
  });

  const hasDeployment = (g: NavGates): boolean =>
    filterNav(NAV, g).some((grp) => grp.items.some((i) => "to" in i && i.to === "/org/deployment"));

  it("hides Deployment on SaaS and for non-admins, shows it for a self-hosted admin", () => {
    expect(hasDeployment(gates({ selfHosted: false }))).toBe(false);
    expect(hasDeployment(gates({ selfHosted: true, orgAdmin: false }))).toBe(false);
    expect(hasDeployment(gates({ selfHosted: true, orgAdmin: true }))).toBe(true);
  });
});

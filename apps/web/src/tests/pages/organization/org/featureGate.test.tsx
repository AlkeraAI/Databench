import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SsoSettingsPage } from "@/pages/organization/org/SsoSettingsPage";
import { AuditLogPage } from "@/pages/organization/org/AuditLogPage";
import { RequireSelfHosted } from "@/app/guards/RequireSelfHosted";
import { filterNav, NAV, type NavGates } from "@/app/nav";
import { orgSettingsTabs } from "@/pages/organization/settings/OrgSettingsTabs";

// The server's gate on SSO and the audit log: an org the dashboard refuses sees the
// unavailable plate in place of the page, and — critically — the now-403 admin API is
// never called (the body that fetches it isn't mounted). An org the server enables gets
// the real page. Driven through the REAL hooks with only `fetch` stubbed, so "never
// fetched" is a real network assertion, not a mocked-away one. With no extension
// installed the plate is the open one; the product's upsell is tested with billing.

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// A complete-enough SSO connection so the real SsoForm renders when un-gated.
const SSO_CONFIG = {
  configured: false,
  enabled: false,
  enforced: false,
  protocol: "oidc",
  allowed_domains: "",
  oidc_issuer: null,
  oidc_client_id: null,
  has_client_secret: false,
  saml_idp_entity_id: null,
  saml_sso_url: null,
  has_saml_cert: false,
  saml_sp_entity_id: "sp",
  saml_acs_url: "acs",
  groups_mapping: {},
  scim_enabled: false,
  has_scim_token: false,
  scim_base_url: "scim",
};

const AUDIT_PAGE = { events: [], total: 0, offset: 0, limit: 50 };

type Call = { method: string; url: string };

function stubFetch(opts: { enterprise?: boolean; selfHosted?: boolean; salesEmail?: string | null }): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = input instanceof Request ? input.url : String(input);
      const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
      calls.push({ method, url });
      const json = (body: unknown, s = 200) =>
        new Response(JSON.stringify(body), { status: s, headers: { "content-type": "application/json" } });
      if (url.includes("/api/v1/config")) {
        return json({
          product_name: "Databench",
          support_email: null,
          sales_email: opts.salesEmail === undefined ? "sales@northwind.test" : opts.salesEmail,
          telemetry_enabled: false,
          self_hosted: opts.selfHosted ?? false,
        });
      }
      if (url.includes("/api/v1/dashboard")) {
        return json({
          user: {},
          org: {},
          teams: [],
          pending_invitations: [],
          is_org_admin: true,
          entitled_features: [],
          enterprise_features_enabled: Boolean(opts.enterprise),
        });
      }
      if (url.includes("/api/v1/org/sso")) return json(SSO_CONFIG);
      if (url.includes("/api/v1/org/audit-events")) return json(AUDIT_PAGE);
      return json({});
    }),
  );
  return calls;
}

function wrap(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const w = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return render(node, { wrapper: w });
}

describe("SSO page — server gate", () => {
  it("says SSO is not turned on, offers no sales contact, and never fetches SSO config when gated", async () => {
    const calls = stubFetch({ enterprise: false });
    wrap(<SsoSettingsPage />);
    expect(await screen.findByText("Single sign-on is not turned on")).toBeInTheDocument();
    expect(screen.getByText("SSO/SAML & SCIM is not available to this organization.")).toBeInTheDocument();
    expect(document.querySelector('a[href^="mailto:"]')).toBeNull();
    // The gated body never mounts, so its (now-403) API is never called.
    expect(calls.some((c) => c.url.includes("/api/v1/org/sso"))).toBe(false);
  });

  it("renders the real form and fetches config when the server enables it", async () => {
    const calls = stubFetch({ enterprise: true });
    wrap(<SsoSettingsPage />);
    // The issuer field proves SsoBody mounted; the upsell is gone.
    expect(await screen.findByRole("textbox", { name: /issuer url/i })).toBeInTheDocument();
    expect(screen.queryByText(/is not turned on/i)).not.toBeInTheDocument();
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/v1/org/sso"))).toBe(true));
  });

  it("shows a retryable error (not the upsell) when the dashboard request fails", async () => {
    // A dashboard failure must not masquerade as "not allowed": show an error + retry,
    // never the unavailable plate — and still never call the SSO API.
    const calls: Call[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
        const url = input instanceof Request ? input.url : String(input);
        calls.push({ method: "GET", url });
        if (url.includes("/api/v1/dashboard")) return new Response("boom", { status: 500 });
        return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
      }),
    );
    wrap(<SsoSettingsPage />);
    expect(await screen.findByText(/could not check your access/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
    expect(screen.queryByText(/is not turned on/i)).not.toBeInTheDocument();
    expect(calls.some((c) => c.url.includes("/api/v1/org/sso"))).toBe(false);
  });
});

describe("Audit page — server gate", () => {
  it("says the audit log is not turned on and never fetches audit events when gated", async () => {
    const calls = stubFetch({ enterprise: false });
    wrap(<AuditLogPage />);
    expect(await screen.findByText("Audit log is not turned on")).toBeInTheDocument();
    expect(calls.some((c) => c.url.includes("/api/v1/org/audit-events"))).toBe(false);
  });

  it("renders the real log and fetches events when the server enables it", async () => {
    const calls = stubFetch({ enterprise: true });
    wrap(<AuditLogPage />);
    expect(await screen.findByText(/no events yet/i)).toBeInTheDocument();
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/v1/org/audit-events"))).toBe(true));
  });
});

describe("RequireSelfHosted guard", () => {
  const renderGuard = (selfHosted: boolean) => {
    stubFetch({ selfHosted });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/org/deployment"]}>
          <Routes>
            <Route element={<RequireSelfHosted />}>
              <Route path="/org/deployment" element={<div>DEPLOY PAGE</div>} />
            </Route>
            <Route path="/" element={<div>HOME PAGE</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  };

  it("redirects to the dashboard on the hosted app", async () => {
    renderGuard(false);
    expect(await screen.findByText("HOME PAGE")).toBeInTheDocument();
    expect(screen.queryByText("DEPLOY PAGE")).not.toBeInTheDocument();
  });

  it("renders the page on a self-hosted install", async () => {
    renderGuard(true);
    expect(await screen.findByText("DEPLOY PAGE")).toBeInTheDocument();
  });

  it("shows the loading gate while public config is still pending", () => {
    // Config never resolves → the guard must hold on GateLoading, not flash the page
    // or the redirect target before the deployment shape is known.
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/org/deployment"]}>
          <Routes>
            <Route element={<RequireSelfHosted />}>
              <Route path="/org/deployment" element={<div>DEPLOY PAGE</div>} />
            </Route>
            <Route path="/" element={<div>HOME PAGE</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("status", { name: /checking your access/i })).toBeInTheDocument();
    expect(screen.queryByText("DEPLOY PAGE")).not.toBeInTheDocument();
    expect(screen.queryByText("HOME PAGE")).not.toBeInTheDocument();
  });
});

describe("Organization nav — SSO + Audit stay reachable", () => {
  const gates = (over: Partial<NavGates>): NavGates => ({
    orgAdmin: true,
    teamAdmin: true,
    platformStaff: false,
    platformAdmin: false,
    selfHosted: false,
    ...over,
  });
  const hasRoute = (g: NavGates, to: string): boolean =>
    filterNav(NAV, g).some((grp) => grp.items.some((i) => "to" in i && i.to === to));
  const tabAt = (to: string) => orgSettingsTabs().find((t) => t.to === to);

  // SSO and the audit log are tabs of Org settings rather than their own sidebar leaves, so the
  // sidebar doorway is the Org settings entry and the tab strip is what carries the two pages.
  it("routes SSO + Audit as org-admin tabs of Org settings", () => {
    expect(tabAt("/settings/organization/sso")).toMatchObject({ orgAdmin: true });
    expect(tabAt("/settings/organization/audit")).toMatchObject({ orgAdmin: true });
  });

  it("keeps the doorway to them visible to an org admin on SaaS and self-hosted", () => {
    for (const selfHosted of [false, true]) {
      expect(hasRoute(gates({ selfHosted }), "/settings/organization")).toBe(true);
    }
  });

  it("hides that doorway from a non-admin, and offers no leaf at the old paths", () => {
    expect(hasRoute(gates({ orgAdmin: false }), "/settings/organization")).toBe(false);
    for (const g of [gates({}), gates({ orgAdmin: false })]) {
      expect(hasRoute(g, "/org/sso")).toBe(false);
      expect(hasRoute(g, "/org/audit")).toBe(false);
    }
  });
});

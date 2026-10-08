// The multi-org pages through the REAL route table and guards.
//
// The no-organization landing is public: a person with no org holds a sign-in but no session
// in any org, so a guard in front of it would bounce them to sign-in, whose sign-in would send
// them back, forever. And nothing on it may renew the session. The single sign-on link page sits
// behind sign-in (returning to itself), and outside the deep-link gate: its `?org=` names the org
// being linked, which the person is not in yet.

import { MemoryRouter, useLocation } from "react-router-dom";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppContent } from "@/App";
import { forgetActiveOrg } from "@/api/activeOrg";
import { queryClient } from "@/api/queryClient";

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const VIEWER = {
  id: "viewer",
  email: "vera@x.io",
  first_name: "Vera",
  last_name: "Ng",
  display_name: "Vera Ng",
  org_team_id: ORG_A,
  org_name: "Acme",
  membership_count: 1,
  email_verified_at: "2026-01-01T00:00:00Z",
  email_verification_required: false,
  email_verification_deadline: null,
  has_password: true,
  admin_team_ids: [],
};

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

let signedIn: boolean;
let multiOrgEnabled: boolean;
let sent: string[];

function route(req: Request): Response {
  const p = new URL(req.url).pathname;
  sent.push(p);
  if (p === "/api/v1/auth/me") {
    return signedIn ? json(VIEWER) : json({ error: { code: "unauthorized", message: "not authenticated", trace_id: "t" } }, 401);
  }
  if (p === "/api/v1/auth/sso-link") return json({ org_team_id: ORG_B, org_name: "Beta Labs", email_masked: "v***@x.io" });
  if (p === "/api/v1/auth/memberships") {
    return json({ active_org_team_id: ORG_A, memberships: [{ org_team_id: ORG_A, org_name: "Acme", role: "member", sso_required: false }] });
  }
  if (p === "/api/v1/auth/oauth/providers") return json({ providers: [] });
  if (p === "/api/v1/config") return json({ self_hosted: false, multi_org_enabled: multiOrgEnabled });
  return json({ detail: `unmatched ${p}` }, 404);
}

function Where() {
  const location = useLocation();
  return <span data-testid="where">{location.pathname + location.search}</span>;
}

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <AppContent />
      <Where />
    </MemoryRouter>,
  );

beforeEach(() => {
  queryClient.clear();
  forgetActiveOrg();
  signedIn = false;
  multiOrgEnabled = true;
  sent = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string, init?: RequestInit) =>
      route(input instanceof Request ? input : new Request(input, init)),
    ),
  );
});
afterEach(() => {
  cleanup();
  forgetActiveOrg();
  vi.unstubAllGlobals();
});

describe("the no-organization landing", () => {
  it("renders signed out and stays put, renewing nothing", async () => {
    renderAt("/no-organization");
    expect(await screen.findByRole("heading", { name: "You're not in any organization" })).toBeInTheDocument();
    await waitFor(() => expect(sent).toContain("/api/v1/auth/me"));
    // Give any guard a chance to bounce it; none does.
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.getByTestId("where")).toHaveTextContent(/^\/no-organization$/);
    expect(sent).not.toContain("/api/v1/auth/refresh");
  });

  it("sends a visitor to sign-in with several orgs per person off", async () => {
    multiOrgEnabled = false;
    renderAt("/no-organization");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(/^\/login$/));
    expect(sent).not.toContain("/api/v1/auth/refresh");
  });

  it("is not where a signed-out person is sent from the app", async () => {
    renderAt("/");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(/^\/login$/));
  });
});

describe("the single sign-on link page", () => {
  it("renders for a signed-in person though its ?org= names an org they are not in", async () => {
    signedIn = true;
    renderAt(`/link-sso?org=${ORG_B}`);
    expect(
      await screen.findByRole("heading", { name: "Link v***@x.io to Beta Labs's single sign-on?" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("You don't have access to this.")).toBeNull();
  });

  it("sends a signed-out person to sign in, returning to itself", async () => {
    renderAt(`/link-sso?org=${ORG_B}`);
    await waitFor(() =>
      expect(screen.getByTestId("where")).toHaveTextContent(
        `/login?return_to=${encodeURIComponent(`/link-sso?org=${ORG_B}`)}`,
      ),
    );
  });
});

import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetActiveOrg, setOrgNavigator } from "@/api/activeOrg";
import { api, request } from "@/api/client";
import { createQueryClient } from "@/api/queryClient";
import { setSessionChannelFactory } from "@/api/sessionChannel";
import { OrgDeepLinkGate } from "@/app/guards/OrgDeepLinkGate";

// A link that names another org holds the page until the reader decides, and nothing under it
// fetches meanwhile; an org the reader is not in gets one line; a link into the current org
// passes; and the return from an org's single sign-on finishes the interrupted switch. Real
// queries and mutations, `fetch` stubbed.

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const STRANGER = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";

const me = {
  id: "11111111-1111-1111-1111-111111111111",
  email: "vera@x.io",
  first_name: "Vera",
  last_name: "Ng",
  display_name: "Vera Ng",
  org_team_id: ORG_A,
  org_name: "Acme",
  org_role: "admin",
  membership_count: 2,
  email_verification_required: false,
  created_at: "2026-01-01T00:00:00Z",
};
const memberships = {
  active_org_team_id: ORG_A,
  memberships: [
    { org_team_id: ORG_A, org_name: "Acme", role: "admin", sso_required: false, last_active_at: null },
    { org_team_id: ORG_B, org_name: "Beta Labs", role: "member", sso_required: false, last_active_at: null },
  ],
};

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

let paths: string[];
let navigated: string[];
let switched: unknown[];
let restore: (url: string) => void;

beforeEach(() => {
  paths = [];
  navigated = [];
  switched = [];
  forgetActiveOrg();
  setSessionChannelFactory(null);
  restore = setOrgNavigator((url) => navigated.push(url));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const path = new URL(input.url).pathname;
      paths.push(path);
      if (path === "/api/v1/auth/me") return json(me);
      if (path === "/api/v1/auth/memberships") return json(memberships);
      if (path === "/api/v1/auth/refresh/org") {
        switched.push(await input.clone().json());
        return json({ user: { org_team_id: ORG_B }, expires_at: new Date(Date.now() + 600_000).toISOString() });
      }
      if (path === "/api/v1/dashboard") return json({ org_name: "Acme" });
      return json({}, 404);
    }),
  );
});

afterEach(() => {
  cleanup();
  setOrgNavigator(restore);
  setSessionChannelFactory(undefined);
  forgetActiveOrg();
  vi.unstubAllGlobals();
});

/** A page that reads the current org's data as soon as it mounts. */
function OrgPage() {
  const dashboard = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => request(api.GET("/api/v1/dashboard")),
  });
  return <p>the page {dashboard.data ? "loaded" : ""}</p>;
}

function renderAt(url: string) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route element={<OrgDeepLinkGate />}>
            <Route path="/files" element={<OrgPage />} />
            <Route path="/" element={<p>the overview</p>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("OrgDeepLinkGate", () => {
  it("holds a link into another of the reader's orgs, fetching nothing beneath it", async () => {
    renderAt(`/files?org=${ORG_B}`);
    expect(await screen.findByText("This is in Beta Labs.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Switch to Beta Labs" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Stay in Acme" })).toBeInTheDocument();
    expect(screen.queryByText(/the page/)).toBeNull();
    expect(paths).not.toContain("/api/v1/dashboard");
  });

  it("switches to the link's org and reloads at the link", async () => {
    renderAt(`/files?org=${ORG_B}`);
    fireEvent.click(await screen.findByRole("button", { name: "Switch to Beta Labs" }));
    await waitFor(() => expect(navigated).toEqual([`/files?org=${ORG_B}`]));
    expect(switched).toEqual([{ org_team_id: ORG_B }]);
  });

  it("stays in the current org at the overview", async () => {
    renderAt(`/files?org=${ORG_B}`);
    fireEvent.click(await screen.findByRole("link", { name: "Stay in Acme" }));
    expect(await screen.findByText("the overview")).toBeInTheDocument();
    expect(paths).not.toContain("/api/v1/dashboard");
  });

  it("says only that the reader has no access to an org they are not in", async () => {
    renderAt(`/files?org=${STRANGER}`);
    expect(await screen.findByText("You don't have access to this.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /switch/i })).toBeNull();
    expect(paths).not.toContain("/api/v1/dashboard");
  });

  it("lets a link into the current org straight through", async () => {
    renderAt(`/files?org=${ORG_A}`);
    expect(await screen.findByText(/the page/)).toBeInTheDocument();
    expect(paths).not.toContain("/api/v1/auth/memberships");
  });

  it("finishes a switch the org's single sign-on interrupted", async () => {
    renderAt(`/?switch_org=${ORG_B}`);
    await waitFor(() => expect(navigated).toEqual(["/"]));
    expect(switched).toEqual([{ org_team_id: ORG_B }]);
  });

  it("drops the parameter when the sign-on already moved the session", async () => {
    renderAt(`/?switch_org=${ORG_A}`);
    expect(await screen.findByText("the overview")).toBeInTheDocument();
    expect(switched).toEqual([]);
  });
});

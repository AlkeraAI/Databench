// The preferences page's paths, through the REAL route table.
//
// The page lives at /preferences. /settings/chat is in bookmarks, in the
// editor's "open your preferences" link, and in links people have shared, so
// its redirect is pinned here rather than assumed.

import { MemoryRouter, useLocation } from "react-router-dom";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppContent } from "@/App";
import { queryClient } from "@/api/queryClient";

const VIEWER = {
  id: "viewer",
  email: "vera@x.io",
  first_name: "Vera",
  last_name: "Ng",
  display_name: "Vera Ng",
  email_verified_at: "2026-01-01T00:00:00Z",
  email_verification_required: false,
  email_verification_deadline: null,
  has_password: true,
};

const ORG = {
  id: "org-1",
  name: "Acme",
  parent_team_id: null,
  is_root: true,
  created_at: "2026-01-01T00:00:00Z",
  member_count: 1,
};

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function route(req: Request): Response {
  const p = new URL(req.url).pathname;
  if (p === "/api/v1/auth/me") return json(VIEWER);
  if (p === "/api/v1/config") return json({ self_hosted: false });
  if (p === "/api/v1/dashboard") {
    return json({ user: VIEWER, org: ORG, teams: [ORG], pending_invitations: [], is_org_admin: false });
  }
  if (p === "/api/v1/me/credits") return json({ tier_key: "pro", tier_name: "Pro", pct_used: 0, reset_at: null, prepaid_credits: 0 });
  if (p === "/api/v1/invitations/me") return json([]);
  if (p === "/api/v1/me/preferences") return json({ preferences: { schema_version: "2.0.0" } });
  if (p === "/api/v1/chat/models") return json({ items: [] });
  return json({ detail: `unmatched ${p}` }, 404);
}

/** The router's current path, read from inside the same router the app renders in. */
function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>;
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
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string, init?: RequestInit) =>
      route(input instanceof Request ? input : new Request(input, init)),
    ),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the preferences route", () => {
  it("renders the reader's preferences at /preferences", async () => {
    renderAt("/preferences");

    expect(await screen.findByRole("button", { name: /default permission mode/i }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Preferences");
  });

  it("sends the old /settings/chat path to /preferences, page and all", async () => {
    renderAt("/settings/chat");

    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/preferences"), {
      timeout: 5000,
    });
    expect(await screen.findByRole("button", { name: /default permission mode/i })).toBeInTheDocument();
  });
});

// `/templates/:templateId` through the REAL route table.
//
// A template's node carries `/templates/<id>` on its object facet, so the drive,
// a copied link and a search result all send people to this address. Two things
// therefore have to hold in the app's own routing rather than in the page: the
// address resolves to the template page instead of falling through to the 404,
// and it resolves BEHIND the session gate — a template is somebody's private
// work, and a signed-out visitor following a pasted link must land on /login
// with the address kept, not on a page that asks the API for a row it will be
// refused.

import { MemoryRouter, useLocation } from "react-router-dom";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppContent } from "@/App";
import { queryClient } from "@/api/queryClient";

const TEMPLATE_ID = "tpl_4";

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

const TEMPLATE = {
  id: TEMPLATE_ID,
  title: "Monthly revenue",
  version: 3,
  owner_user_id: "viewer",
  created_at: "2026-02-01T09:00:00Z",
  updated_at: "2026-02-09T17:30:00Z",
  files_node_id: "nd_tpl",
  brief: "Pull last month's revenue by region.",
  model: null,
  permission_mode: "read_only",
  source_chat_id: null,
  saved_from_seq: 0,
};

/** Signed in unless a test says otherwise. */
let signedIn = true;

const json = (body: unknown, status = 200): Response =>
  new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

function route(req: Request): Response {
  const p = new URL(req.url).pathname;
  if (p === "/api/v1/auth/me") return signedIn ? json(VIEWER) : json({ detail: "no" }, 401);
  if (p === "/api/v1/config") return json({ self_hosted: false });
  if (p === "/api/v1/dashboard") {
    return json({
      user: VIEWER,
      org: ORG,
      teams: [ORG],
      pending_invitations: [],
      is_org_admin: false,
    });
  }
  if (p === "/api/v1/me/credits") {
    return json({ tier_key: "pro", tier_name: "Pro", pct_used: 0, reset_at: null, prepaid_credits: 0 });
  }
  if (p === "/api/v1/invitations/me") return json([]);
  if (p === `/api/v1/chat-templates/${TEMPLATE_ID}`) return json(TEMPLATE);
  if (p === "/api/v1/files/drives") {
    return json({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
  }
  if (p.includes("/items/")) {
    return json({
      id: "nd_tpl",
      driveId: "dr_1",
      kind: "folder",
      name: "Monthly revenue.alkerachat.template",
      nameDisplay: "Monthly revenue.alkerachat.template",
      etag: "et_1",
      capabilities: { can_read: true, can_write: true, can_share: true, refusals: {} },
      object: {
        type: "chat_template",
        id: TEMPLATE_ID,
        title: "Monthly revenue",
        web_url: `/templates/${TEMPLATE_ID}`,
        metadata: { files_node_id: "nd_scratch" },
      },
    });
  }
  return json({ detail: `unmatched ${p}` }, 404);
}

/** The router's current path + query, read from inside the app's own router. */
function Where() {
  const location = useLocation();
  return <span data-testid="where">{`${location.pathname}${location.search}`}</span>;
}

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <AppContent />
      <Where />
    </MemoryRouter>,
  );

beforeEach(() => {
  signedIn = true;
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

describe("the chat template route", () => {
  it("renders the template at /templates/:templateId", async () => {
    renderAt(`/templates/${TEMPLATE_ID}`);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Monthly revenue" }, { timeout: 5000 }),
    ).toBeInTheDocument();
    // Inside the shell, not a bare page: the nav is what makes a template reachable
    // from anywhere else a person goes next.
    expect(screen.getByRole("navigation")).toBeInTheDocument();
  });

  it("sends a signed-out visitor to sign in, keeping the address", async () => {
    signedIn = false;
    renderAt(`/templates/${TEMPLATE_ID}`);

    await waitFor(
      () =>
        expect(screen.getByTestId("where").textContent).toBe(
          `/login?return_to=${encodeURIComponent(`/templates/${TEMPLATE_ID}`)}`,
        ),
      { timeout: 5000 },
    );
    expect(screen.queryByRole("heading", { name: "Monthly revenue" })).toBeNull();
  });
});

import { MemoryRouter, useLocation } from "react-router-dom";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppContent } from "@/App";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { queryClient } from "@/api/queryClient";

// The app's fall-through route, driven through the REAL route table (AppContent) with only `fetch`
// stubbed. A path no route claims lands inside the
// signed-in shell on a page that names itself and links back home, and a signed-out visitor at the
// same path is still sent through the auth guard.
//
// Every expected reading is read off the NotFoundPage component itself, so the route pin cannot
// drift from the page's own wording.

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

function route(req: Request, signedIn: boolean): Response {
  const p = new URL(req.url).pathname;
  if (p === "/api/v1/auth/me") return signedIn ? json(VIEWER) : json({ detail: "no session" }, 401);
  if (p === "/api/v1/dashboard") {
    return json({ user: VIEWER, org: ORG, teams: [ORG], pending_invitations: [], is_org_admin: false });
  }
  if (p === "/api/v1/me/credits") {
    return json({ tier_key: "pro", tier_name: "Pro", pct_used: 10, reset_at: null, prepaid_credits: 0 });
  }
  // The dashboard's other two live sources, answered so a redirect that lands on home settles
  // instead of retrying into the test's teardown.
  if (p === "/api/v1/me/usage") return json({ window: "30d", total_requests: 0, by_model: [], daily: [] });
  if (p === "/api/v1/kb/browse") return json({ items: [], total: 0 });
  if (p === "/api/v1/invitations/me") return json([]);
  if (p === "/api/v1/config") return json({ self_hosted: false });
  return json({ detail: `unmatched ${p}` }, 404);
}

function stubFetch(signedIn: boolean) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string, init?: RequestInit) =>
      route(input instanceof Request ? input : new Request(input, init), signedIn),
    ),
  );
}

/** Reports the resolved location, so a guard's redirect target and the deep link it carries are
 *  readable from outside AppContent, which owns its own <Routes>. */
function LocationProbe({ onResolve }: { onResolve: (href: string) => void }) {
  const loc = useLocation();
  onResolve(loc.pathname + loc.search);
  return null;
}

let landed = "";

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <LocationProbe onResolve={(href) => (landed = href)} />
      <AppContent />
    </MemoryRouter>,
  );

// The page's own heading, read from the component instead of copied, cached so the probe render
// happens once. Its `Link` needs a router; nothing else about it is stubbed.
let cachedHeading: string | null = null;
function notFoundHeading(): string {
  if (cachedHeading === null) {
    const probe = render(
      <MemoryRouter>
        <NotFoundPage />
      </MemoryRouter>,
    );
    cachedHeading = probe.container.querySelector("h1")?.textContent ?? "";
    probe.unmount();
  }
  return cachedHeading;
}

beforeEach(() => {
  landed = "";
  queryClient.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const findNotFound = () =>
  screen.findByRole("heading", { level: 1, name: notFoundHeading() }, { timeout: 5000 });

describe("an unclaimed path", () => {
  // A single segment, a deep path, one shaped like the legacy vocabulary, and one segment too many
  // under a real route: no route claims any of them, so all four fall through.
  it.each([
    ["a single segment", "/no-such-destination"],
    ["a deep path", "/no/such/destination"],
    ["a legacy-looking path", "/dashboard/no-such-destination"],
    ["an extra segment under a real route", "/teams/root/no-such-destination"],
  ])("resolves to the not-found page: %s", async (_case, path) => {
    stubFetch(true);
    // An empty heading would make the name match vacuous.
    expect(notFoundHeading()).not.toBe("");
    renderAt(path);
    expect(await findNotFound()).toBeInTheDocument();
  });

  it("stays inside the app shell and offers a way back to the workspace", async () => {
    // Not a bare page: the sidebar nav is still there to navigate away with, and the surface
    // itself carries a way home, unlike the dead-end stand-in it replaced. The link assert is
    // scoped to the surface, since the shell's nav links home too.
    stubFetch(true);
    renderAt("/no-such-destination");
    const surface = (await findNotFound()).parentElement!;
    expect(await screen.findAllByRole("navigation")).not.toHaveLength(0);
    expect(within(surface).getByRole("link")).toHaveAttribute("href", "/");
  });

  it("sends a signed-out visitor through the auth guard with the attempted path", async () => {
    // The fall-through route sits INSIDE RequireAuth, so an unclaimed path is not a public surface:
    // it redirects to login and hands the attempted location on as a deep link.
    stubFetch(false);
    renderAt("/no-such-destination");
    await waitFor(
      () => expect(landed).toBe(`/login?return_to=${encodeURIComponent("/no-such-destination")}`),
      { timeout: 5000 },
    );
    expect(screen.queryByRole("heading", { level: 1, name: notFoundHeading() })).not.toBeInTheDocument();
  });
});

describe("an unclaimed path under /admin", () => {
  // Which answer comes first for an ordinary member: "no such page" or "not your area"? The
  // fall-through route lives in the member block, a sibling of the platform-staff block, so an
  // unclaimed /admin path never reaches RequirePlatformStaff and the member reads the same
  // not-found surface they get anywhere else. The pair below is what makes that an ORDERING
  // claim rather than a claim that the staff guard is inert: same viewer, same prefix, one
  // path the staff block claims and one it does not.
  //
  // The visible consequence: an unclaimed admin path holds its URL, while a real one bounces
  // home, so a member can still tell an admin page exists by watching where they land.

  it("shows a member the not-found surface, not the staff guard's bounce", async () => {
    stubFetch(true);
    renderAt("/admin/no-such-console");
    expect(await findNotFound()).toBeInTheDocument();
    expect(landed).toBe("/admin/no-such-console");
  });

  it("but sends that same member home from an admin path the staff block claims", async () => {
    // The discriminating twin. VIEWER carries no `platform_role`, so the guard rejects them here.
    stubFetch(true);
    renderAt("/admin/orgs");
    await waitFor(() => expect(landed).toBe("/"), { timeout: 5000 });
    expect(screen.queryByRole("heading", { level: 1, name: notFoundHeading() })).not.toBeInTheDocument();
  });
});

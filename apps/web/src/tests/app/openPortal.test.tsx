// The open portal as the public repository builds it: the open extensions installed
// (src/open/portal.ts) and nothing private. The private pages are not there and nothing in
// the shell names them, while the open extensions' pages are; the route table, the sidebar,
// the org settings strip and the page titles are pinned through the REAL modules.

import { MemoryRouter } from "react-router-dom";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { installExtensions } from "@alkera/ui/extensions";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { AppContent } from "@/App";
import { queryClient } from "@/api/queryClient";
import { sectionTitleFor } from "@/app/documentTitle";
import { hasChildren, portalNav } from "@/app/nav";
import { PORTAL_EXTENSIONS } from "@/open/portal";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { orgSettingsTabs } from "@/pages/organization/settings/OrgSettingsTabs";

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

/** Every path the portal asked for that the open backend does not serve. */
let unserved: string[] = [];

function route(req: Request): Response {
  const p = new URL(req.url).pathname;
  const answer = openRoute(p);
  if (answer.status === 404) unserved.push(p);
  return answer;
}

/** The open backend, as far as the shell and the overview read it. Anything else is a 404,
 *  which is what the open backend answers for a private route. */
function openRoute(p: string): Response {
  if (p === "/api/v1/auth/me") return json(VIEWER);
  if (p === "/api/v1/config") return json({ self_hosted: false });
  if (p === "/api/v1/dashboard") {
    return json({ user: VIEWER, org: ORG, teams: [ORG], pending_invitations: [], is_org_admin: true });
  }
  if (p === "/api/v1/invitations/me") return json([]);
  // The live event stream: open, and an empty stream is all the overview needs of it.
  if (p === "/api/v1/events") return new Response("", { status: 200, headers: { "content-type": "text/event-stream" } });
  if (p === "/api/v1/me/connections") return json([]);
  if (p === "/api/v1/org/machines") return json([]);
  if (p === "/api/v1/chats") {
    return json({
      items: [{ id: "ch1", title: "Churn cohort", owner_user_id: VIEWER.id, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-02T00:00:00Z", last_seq: 0 }],
      next_cursor: null,
    });
  }
  return json({ detail: `unmatched ${p}` }, 404);
}

/** The not-found page's own heading, read off the component so the pin cannot drift from it. */
function notFoundHeading(): string {
  const probe = render(
    <MemoryRouter>
      <NotFoundPage />
    </MemoryRouter>,
  );
  const heading = probe.container.querySelector("h1")?.textContent ?? "";
  probe.unmount();
  return heading;
}

beforeAll(() => {
  installExtensions(PORTAL_EXTENSIONS);
});

beforeEach(() => {
  queryClient.clear();
  unserved = [];
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

describe("the open portal's route table", () => {
  it.each([
    ["knowledge", "/knowledge"],
    ["the graph page", "/graph"],
    ["the plan page", "/settings/billing"],
    ["the org billing tab", "/settings/organization/billing"],
    ["the usage page", "/analytics/usage"],
  ])("does not route %s", async (_what, path) => {
    const heading = notFoundHeading();
    expect(heading).not.toBe("");
    render(
      <MemoryRouter initialEntries={[path]}>
        <AppContent />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { level: 1, name: heading }, { timeout: 5000 })).toBeInTheDocument();
  });

  it("routes the open extensions' pages", async () => {
    const heading = notFoundHeading();
    render(
      <MemoryRouter initialEntries={["/connections"]}>
        <AppContent />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { level: 1 }, { timeout: 5000 })).not.toHaveTextContent(heading);
  });

  it("still routes its own pages, so the not-found answers above are not the shell failing", async () => {
    render(
      <MemoryRouter initialEntries={["/settings/organization/sso"]}>
        <AppContent />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("tab", { name: /single sign-on/i }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /billing/i })).toBeNull();
  });
});

describe("the open portal's shell", () => {
  it("offers the open sidebar alone", () => {
    const leaves = portalNav().flatMap((group) =>
      group.items.flatMap((item) => (hasChildren(item) ? item.children : [item])).map((leaf) => leaf.to),
    );
    expect(leaves).toContain("/chat");
    expect(leaves).toContain("/files");
    expect(leaves).toContain("/connections");
    for (const privatePath of [
      "/knowledge",
      "/org/gate",
      "/org/integration",
      "/analytics/usage",
      "/admin/enterprise",
      "/admin/models",
      "/admin/gateway-cap",
    ]) {
      expect(leaves).not.toContain(privatePath);
    }
    expect(portalNav().map((group) => group.group)).toEqual(["Personal", "Organization", "Platform"]);
  });

  it("offers the open org settings tabs alone", () => {
    expect(orgSettingsTabs().map((tab) => tab.key)).toEqual(["general", "sso", "audit"]);
  });

  it("names no private page in a title", () => {
    expect(sectionTitleFor("/settings/billing")).toBeNull();
    expect(sectionTitleFor("/graph")).toBeNull();
    expect(sectionTitleFor("/org/gate/runs/run-1")).toBeNull();
  });
});

describe("the open overview", () => {
  it("loads from open routes alone, with no error and no request the open backend would refuse", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <AppContent />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("link", { name: "Churn cohort" }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connections: 0" })).toBeInTheDocument();
    expect(screen.queryByText(/couldn.t load your overview/i)).toBeNull();
    // The private cards and the catalog search are not there, and no hole is left for them.
    expect(screen.queryByText("Usage by model")).toBeNull();
    expect(screen.queryByRole("searchbox", { name: /search the workspace/i })).toBeNull();
    await waitFor(() => expect(unserved).toEqual([]));
  });

  it("offers no plan row in the account menu", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <AppContent />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /vera ng/i }, { timeout: 5000 }));
    const menu = screen.getByRole("menu", { name: "Account" });
    expect(within(menu).queryByRole("menuitem", { name: /manage plan/i })).toBeNull();
    expect(unserved).toEqual([]);
  });
});

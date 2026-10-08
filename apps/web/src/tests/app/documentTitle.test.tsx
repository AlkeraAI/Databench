// What the browser tab says, driven through the REAL route table.
//
// The tab is the one piece of the product a person reads while looking at ANOTHER
// window — a row of open tabs is how they find their way back. So the pins here are
// the three things that make it useful: the page is named, the name is specific
// (this chat, this settings tab — not "Chat"), and the brand is whatever the
// deployment configured, never a literal.
//
// The section is deliberately the same string the masthead shows, which is why the
// last test mutates the shared map and watches BOTH move: two sources would drift
// the moment someone renames a page in the nav.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppContent } from "@/App";
import { keys } from "@/api/keys";
import { queryClient } from "@/api/queryClient";
import { PATH_TITLES, composeDocumentTitle, sectionTitleFor, specificFromCache } from "@/app/documentTitle";

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

/** The product name the deployment is configured with — never spelled in the app. */
let productName: string | null = "Alkera";
/** Whether /api/v1/config has answered yet (a deployment's brand arrives late). */
let configAnswers = true;

function route(req: Request): Response {
  const p = new URL(req.url).pathname;
  if (p === "/api/v1/auth/me") return json(VIEWER);
  if (p === "/api/v1/config") {
    return json(productName ? { self_hosted: false, product_name: productName } : { self_hosted: false });
  }
  if (p === "/api/v1/dashboard") {
    return json({ user: VIEWER, org: ORG, teams: [ORG], pending_invitations: [], is_org_admin: true });
  }
  if (p === "/api/v1/me/credits") {
    return json({ tier_key: "pro", tier_name: "Pro", pct_used: 0, reset_at: null, prepaid_credits: 0 });
  }
  if (p === "/api/v1/invitations/me") return json([]);
  if (p === "/api/v1/me/preferences") return json({ preferences: { schema_version: "2.0.0" } });
  if (p === "/api/v1/chat/models") return json({ items: [] });
  if (p === "/api/v1/org/settings") return json({ google_enabled: true, github_enabled: true });
  if (p === "/api/v1/org/sync-settings") {
    return json({ sync_enabled: false, default_visibility: "private", promotion_policy: "manual" });
  }
  if (p === "/api/v1/org/billing") {
    return json({
      org_id: "org-1",
      name: "Acme",
      credit_issuance_enabled: false,
      billing_mode: "stripe",
      pool_account_id: "acct_pool",
      pool_granted_nanos: 0,
      pool_consumed_nanos: 0,
      pool_reserved_nanos: 0,
      pool_available_nanos: 0,
      enrolled: true,
      period_start: "2026-06-03T00:00:00Z",
      period_end: "2026-07-03T00:00:00Z",
      postpaid_consumed_period_nanos: 0,
      purchase_enabled: true,
      purchase_pending: false,
      team_pools: [],
      members: [],
    });
  }
  return json({ detail: `unmatched ${p}` }, 404);
}

/** A doorway out of the page under test, so "navigating away" is a real navigation. */
function Go({ to }: { to: string }) {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(to)}>
      go-elsewhere
    </button>
  );
}

const renderAt = (path: string, extra?: React.ReactNode) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <AppContent />
      {extra}
    </MemoryRouter>,
  );

beforeEach(() => {
  productName = "Alkera";
  configAnswers = true;
  document.title = "pre-hydration";
  queryClient.clear();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string, init?: RequestInit) => {
      const req = input instanceof Request ? input : new Request(input, init);
      if (!configAnswers && new URL(req.url).pathname === "/api/v1/config") {
        // A request that never settles — the app is up, the brand is not known yet.
        return new Promise<Response>(() => {});
      }
      return route(req);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("what the tab says on each kind of page", () => {
  it.each([
    ["/", "Overview · Alkera"],
    ["/files", "Files · Alkera"],
    ["/chat", "Chat · Alkera"],
    ["/preferences", "Preferences · Alkera"],
    ["/settings/organization", "Organization settings · Alkera"],
    ["/settings/organization/sso", "Single sign-on · Organization settings · Alkera"],
    ["/login", "Sign in · Alkera"],
    ["/signup", "Create your account · Alkera"],
    ["/forgot-password", "Reset your password · Alkera"],
    ["/nothing/here", "Not found · Alkera"],
  ])("names %s", async (path, expected) => {
    renderAt(path);
    await waitFor(() => expect(document.title).toBe(expected), { timeout: 5000 });
  });

  it("carries the deployment's own product name, not a literal", async () => {
    productName = "Northwind Data";
    renderAt("/files");
    await waitFor(() => expect(document.title).toBe("Files · Northwind Data"), { timeout: 5000 });
  });

  it("names the section alone while the public config has not answered", async () => {
    configAnswers = false;
    renderAt("/files");
    await waitFor(() => expect(document.title).toBe("Files"), { timeout: 5000 });
    // And nothing has guessed a brand in the meantime.
    expect(document.title).not.toContain("·");
  });

  it("releases a page's own name when the reader navigates away", async () => {
    const user = userEvent.setup();
    renderAt("/settings/organization/sso", <Go to="/preferences" />);
    await waitFor(() => expect(document.title).toBe("Single sign-on · Organization settings · Alkera"), {
      timeout: 5000,
    });

    await user.click(screen.getByRole("button", { name: "go-elsewhere" }));
    await waitFor(() => expect(document.title).toBe("Preferences · Alkera"), { timeout: 5000 });
  });
});

describe("two pages the nav labels alike are named apart", () => {
  it("calls the console's landing page something other than the workspace Overview", () => {
    expect(sectionTitleFor("/")).toBe("Overview");
    expect(sectionTitleFor("/admin")).toBe("Admin overview");
    // Only the landing page: the console's other pages keep their own nav names.
    expect(sectionTitleFor("/admin/ops")).toBe("Ops");
    // Machines is its own page under the org-settings path, and so is a machine's page.
    expect(sectionTitleFor("/settings/organization/machines")).toBe("Machines");
    expect(sectionTitleFor("/settings/organization/machines/m-1")).toBe("Machines");
    expect(sectionTitleFor("/settings/organization/billing")).toBe("Organization settings");
  });
});

describe("the section is the masthead's, not a second copy of it", () => {
  it("moves the tab and the masthead together when a page is renamed", async () => {
    const original = PATH_TITLES["/preferences"];
    PATH_TITLES["/preferences"] = "My settings";
    try {
      renderAt("/preferences");
      await waitFor(() => expect(document.title).toBe("My settings · Alkera"), { timeout: 5000 });
      expect(await screen.findByText("My settings", {}, { timeout: 5000 })).toBeInTheDocument();
    } finally {
      PATH_TITLES["/preferences"] = original;
    }
  });
});

describe("a page about one thing, read from the cache the page already filled", () => {
  const chatRow = (title: string) => ({
    items: [
      {
        id: "c1",
        title,
        owner_user_id: "viewer",
        machine_id: null,
        machine_status: "none",
        machine_refusal_reason: null,
        wake_requested_at: null,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        last_seq: 0,
      },
    ],
    next_cursor: null,
  });

  it("names the chat the reader has open", () => {
    queryClient.setQueryData(keys.chats.all, chatRow("What connections do you see?"));
    expect(specificFromCache(queryClient, "/chat/c1")).toBe("What connections do you see?");
    expect(
      composeDocumentTitle(specificFromCache(queryClient, "/chat/c1"), "Chat", "Alkera"),
    ).toBe("What connections do you see? · Chat · Alkera");
  });

  it("follows a rename — the tab is not a snapshot of the name at open time", () => {
    queryClient.setQueryData(keys.chats.all, chatRow("Untitled"));
    expect(specificFromCache(queryClient, "/chat/c1")).toBe("Untitled");
    queryClient.setQueryData(keys.chats.all, chatRow("Warehouse cost audit"));
    expect(specificFromCache(queryClient, "/chat/c1")).toBe("Warehouse cost audit");
  });

  it("prefers the chat's own row over the list when both are cached", () => {
    queryClient.setQueryData(keys.chats.all, chatRow("Stale list copy"));
    queryClient.setQueryData(keys.chats.one("c1"), { id: "c1", title: "Fresh" });
    expect(specificFromCache(queryClient, "/chat/c1")).toBe("Fresh");
  });

  it("names a Files folder, and the trash is not a folder id", () => {
    queryClient.setQueryData(keys.files.item("n1"), { id: "n1", name: "Quarterly close" });
    expect(specificFromCache(queryClient, "/files/n1")).toBe("Quarterly close");
    expect(specificFromCache(queryClient, "/files/trash")).toBeNull();
    expect(specificFromCache(queryClient, "/files")).toBeNull();
  });

  it("names a chat opened in Files by the chat's title, and follows a rename", () => {
    // A chat is a `<Title>.alkerachat` FOLDER whose filesystem name was minted
    // once from the title and never rewritten, so the tab must read what every
    // other surface reads — the object's CURRENT title.
    const chatFolder = (title: string) => ({
      id: "n2",
      name: "3952c9e2-4d6a-4a01-9f53-000000000001.alkerachat",
      nameDisplay: "3952c9e2-4d6a-4a01-9f53-000000000001.alkerachat",
      object: { id: "obj_1", type: "chat", title },
    });
    queryClient.setQueryData(keys.files.item("n2"), chatFolder("Untitled"));
    expect(specificFromCache(queryClient, "/files/n2")).toBe("Untitled");

    // Renamed: the cache row is refreshed, and the tab moves with it.
    queryClient.setQueryData(keys.files.item("n2"), chatFolder("Warehouse cost audit"));
    expect(specificFromCache(queryClient, "/files/n2")).toBe("Warehouse cost audit");
    expect(specificFromCache(queryClient, "/files/n2")).not.toContain("alkerachat");
  });

  it("says nothing about a chat it has never read", () => {
    expect(specificFromCache(queryClient, "/chat/c1")).toBeNull();
    expect(specificFromCache(queryClient, "/chat")).toBeNull();
  });

  it("names the chat on its sub-routes too", () => {
    queryClient.setQueryData(keys.chats.all, chatRow("Data audit"));
    expect(specificFromCache(queryClient, "/chat/c1/results")).toBe("Data audit");
    expect(specificFromCache(queryClient, "/chat/c1/plan/p1")).toBe("Data audit");
  });
});

describe("composing the title", () => {
  it("trims a long name rather than filling the tab with one chat", () => {
    const long = "A".repeat(300);
    const title = composeDocumentTitle(long, "Chat", "Alkera");
    const thing = title.split(" · ")[0];
    expect(thing.length).toBeLessThanOrEqual(80);
    expect(thing.endsWith("…")).toBe(true);
    expect(title.endsWith(" · Chat · Alkera")).toBe(true);
  });

  it("keeps a name that fits exactly as written", () => {
    expect(composeDocumentTitle("Q3 spend", "Chat", "Alkera")).toBe("Q3 spend · Chat · Alkera");
  });

  it("flattens a multi-line name into one line", () => {
    expect(composeDocumentTitle("  Two\nlines  ", "Chat", "Alkera")).toBe("Two lines · Chat · Alkera");
  });

  it("says the section once when the page's subject IS the section", () => {
    expect(composeDocumentTitle("Files", "Files", "Alkera")).toBe("Files · Alkera");
  });

  it("drops whatever is absent instead of leaving empty separators", () => {
    expect(composeDocumentTitle(null, "Files", "Alkera")).toBe("Files · Alkera");
    expect(composeDocumentTitle("Billing", "Organization settings", null)).toBe(
      "Billing · Organization settings",
    );
    expect(composeDocumentTitle(null, null, null)).toBe("");
  });
});

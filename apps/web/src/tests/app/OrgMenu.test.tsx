import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { enterOrg, forgetActiveOrg, setOrgNavigator } from "@/api/activeOrg";
import { createQueryClient } from "@/api/queryClient";
import { setSessionChannelFactory } from "@/api/sessionChannel";
import { CreateOrgDialog, OrgMenuSection } from "@/app/OrgMenu";

// The account menu's org rows and the create dialog, through the real hooks with only `fetch`
// stubbed. The orgs live in the "Switch organization" flyout: every org, the current one listed
// but not offered. Pending orgs and "Create organization" exist only where the public config says
// the server runs with several orgs per person; with it off a person in one org gets no org rows
// and no memberships read. A pending org reads Join and never switches; joining it moves it into
// the flyout once the list refreshes. A create switches into exactly the org it made; its
// refusals are said inline.

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const ORG_P = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const ORG_N = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";

const row = (org: string, name: string, status: "active" | "pending" = "active") => ({
  org_team_id: org,
  org_name: name,
  role: "member",
  sso_required: false,
  last_active_at: null,
  status,
});

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const refusal = (status: number, code: string, message: string) =>
  json({ error: { code, message, trace_id: "t" } }, status);

let sent: Request[];
let navigated: string[];
let restore: (url: string) => void;
let memberships: () => unknown;
let createAnswer: () => Response;
let joinAnswer: () => Response;
let multiOrgEnabled: boolean;

function serve() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      sent.push(input.clone());
      const path = new URL(input.url).pathname;
      if (path === "/api/v1/config") return json({ self_hosted: false, multi_org_enabled: multiOrgEnabled });
      if (path === "/api/v1/auth/memberships") return json(memberships());
      if (path === "/api/v1/auth/memberships/join") return joinAnswer();
      if (path === "/api/v1/orgs") return createAnswer();
      if (path === "/api/v1/auth/refresh/org") {
        return json({ user: { org_team_id: ORG_N }, expires_at: new Date(Date.now() + 600_000).toISOString() });
      }
      return json({}, 404);
    }),
  );
}

/** Open the "Switch organization" flyout and answer its rows' names once the list has loaded. */
async function openFlyout(menu: HTMLElement, settled: string) {
  fireEvent.click(await within(menu).findByRole("menuitem", { name: "Switch organization" }));
  const flyout = await screen.findByRole("menu", { name: "Switch organization" });
  await within(flyout).findByRole("menuitem", { name: settled });
  return flyout;
}
const names = (flyout: HTMLElement) => within(flyout).getAllByRole("menuitem").map((i) => i.textContent);

const posted = (path: string) => sent.filter((r) => r.method === "POST" && new URL(r.url).pathname === path);

function renderMenu(user: { org_team_id: string; membership_count: number }, onCreate = vi.fn()) {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <div role="menu" aria-label="Account">
          <OrgMenuSection user={user} close={() => undefined} onCreate={onCreate} />
        </div>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { menu: screen.getByRole("menu", { name: "Account" }), onCreate };
}

function renderDialog() {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <CreateOrgDialog open onClose={() => undefined} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  sent = [];
  navigated = [];
  multiOrgEnabled = true;
  memberships = () => ({ active_org_team_id: ORG_A, memberships: [row(ORG_A, "Acme")] });
  createAnswer = () => json({ org_team_id: ORG_N, org_name: "Northwind" }, 201);
  joinAnswer = () => json(row(ORG_P, "Pending Co"));
  setSessionChannelFactory(null);
  restore = setOrgNavigator((url) => navigated.push(url));
  forgetActiveOrg();
  enterOrg(ORG_A);
  serve();
});

afterEach(() => {
  cleanup();
  setOrgNavigator(restore);
  setSessionChannelFactory(undefined);
  forgetActiveOrg();
  vi.unstubAllGlobals();
});

describe("the account menu's pending orgs", () => {
  it("lists a pending org for a person in one org, as Join, never under switch", async () => {
    memberships = () => ({ active_org_team_id: ORG_A, memberships: [row(ORG_A, "Acme"), row(ORG_P, "Pending Co", "pending")] });
    const { menu } = renderMenu({ org_team_id: ORG_A, membership_count: 1 });
    const pending = await within(menu).findByRole("group", { name: "Pending organizations" });
    expect(within(pending).getByRole("menuitem", { name: "Join Pending Co" })).toBeInTheDocument();
    expect(names(await openFlyout(menu, "Acme"))).toEqual(["Acme", "Create organization"]);
  });

  it("keeps a pending org out of the switch list of a person in several orgs", async () => {
    memberships = () => ({
      active_org_team_id: ORG_A,
      memberships: [row(ORG_A, "Acme"), row(ORG_B, "Beta Labs"), row(ORG_P, "Pending Co", "pending")],
    });
    const { menu } = renderMenu({ org_team_id: ORG_A, membership_count: 2 });
    expect(names(await openFlyout(menu, "Beta Labs"))).toEqual(["Acme", "Beta Labs", "Create organization"]);
  });

  it("joins, and the joined org moves into the switch list once the list refreshes", async () => {
    let joinedYet = false;
    memberships = () => ({
      active_org_team_id: ORG_A,
      memberships: [row(ORG_A, "Acme"), row(ORG_B, "Beta Labs"), row(ORG_P, "Pending Co", joinedYet ? "active" : "pending")],
    });
    joinAnswer = () => {
      joinedYet = true;
      return json(row(ORG_P, "Pending Co"));
    };
    const { menu } = renderMenu({ org_team_id: ORG_A, membership_count: 2 });
    fireEvent.click(await within(menu).findByRole("menuitem", { name: "Join Pending Co" }));
    await waitFor(() => expect(within(menu).queryByRole("group", { name: "Pending organizations" })).toBeNull());
    expect(await posted("/api/v1/auth/memberships/join")[0].json()).toEqual({ org_team_id: ORG_P });
    expect(names(await openFlyout(menu, "Pending Co"))).toEqual(["Acme", "Beta Labs", "Pending Co", "Create organization"]);
    // Joining switched nothing.
    expect(posted("/api/v1/auth/refresh/org")).toHaveLength(0);
  });

  it("says a refused join in the menu", async () => {
    memberships = () => ({ active_org_team_id: ORG_A, memberships: [row(ORG_A, "Acme"), row(ORG_P, "Pending Co", "pending")] });
    joinAnswer = () => refusal(409, "login_method_not_allowed", "This organization doesn't allow this sign-in method.");
    const { menu } = renderMenu({ org_team_id: ORG_A, membership_count: 1 });
    fireEvent.click(await within(menu).findByRole("menuitem", { name: "Join Pending Co" }));
    expect(await within(menu).findByRole("alert")).toHaveTextContent("This organization doesn't allow this sign-in method.");
  });
});

describe("the account menu's Create organization", () => {
  it("is offered to a person in one org when the server runs with several orgs per person", async () => {
    const { menu, onCreate } = renderMenu({ org_team_id: ORG_A, membership_count: 1 });
    const flyout = await openFlyout(menu, "Create organization");
    fireEvent.click(within(flyout).getByRole("menuitem", { name: "Create organization" }));
    expect(onCreate).toHaveBeenCalledTimes(1);
  });
});

describe("the account menu's switch flyout", () => {
  it("lists the current org disabled and marked current", async () => {
    memberships = () => ({ active_org_team_id: ORG_A, memberships: [row(ORG_A, "Acme"), row(ORG_B, "Beta Labs")] });
    const { menu } = renderMenu({ org_team_id: ORG_A, membership_count: 2 });
    const flyout = await openFlyout(menu, "Acme");
    const current = within(flyout).getByRole("menuitem", { name: "Acme" });
    expect(current).toBeDisabled();
    expect(current).toHaveAttribute("aria-current", "true");
    expect(within(flyout).getByRole("menuitem", { name: "Beta Labs" })).not.toHaveAttribute("aria-current");
  });

  it("sends the switch for exactly the org picked, and none for the current one", async () => {
    memberships = () => ({ active_org_team_id: ORG_A, memberships: [row(ORG_A, "Acme"), row(ORG_B, "Beta Labs")] });
    const { menu } = renderMenu({ org_team_id: ORG_A, membership_count: 2 });
    const flyout = await openFlyout(menu, "Beta Labs");
    fireEvent.click(within(flyout).getByRole("menuitem", { name: "Acme" }));
    expect(posted("/api/v1/auth/refresh/org")).toHaveLength(0);
    fireEvent.click(within(flyout).getByRole("menuitem", { name: "Beta Labs" }));
    await waitFor(() => expect(posted("/api/v1/auth/refresh/org")).toHaveLength(1));
    const switched = posted("/api/v1/auth/refresh/org")[0];
    expect(await switched.json()).toEqual({ org_team_id: ORG_B });
    expect(switched.headers.get("X-Requested-With")).toBe("alkera");
  });
});

describe("the account menu with several orgs per person off", () => {
  async function configAnswered() {
    await waitFor(() => expect(sent.some((r) => new URL(r.url).pathname === "/api/v1/config")).toBe(true));
    // Let the answer render.
    await new Promise((r) => setTimeout(r, 20));
  }

  it("shows a person in one org nothing new and reads no memberships", async () => {
    multiOrgEnabled = false;
    // Even a list that somehow carried a pending org is never asked for.
    memberships = () => ({ active_org_team_id: ORG_A, memberships: [row(ORG_A, "Acme"), row(ORG_P, "Pending Co", "pending")] });
    const { menu } = renderMenu({ org_team_id: ORG_A, membership_count: 1 });
    await configAnswered();
    expect(within(menu).queryAllByRole("menuitem")).toHaveLength(0);
    expect(sent.some((r) => new URL(r.url).pathname === "/api/v1/auth/memberships")).toBe(false);
  });

  it("keeps the switch list of a person in several orgs and adds no pending org or create", async () => {
    multiOrgEnabled = false;
    memberships = () => ({
      active_org_team_id: ORG_A,
      memberships: [row(ORG_A, "Acme"), row(ORG_B, "Beta Labs"), row(ORG_P, "Pending Co", "pending")],
    });
    const { menu } = renderMenu({ org_team_id: ORG_A, membership_count: 2 });
    await configAnswered();
    expect(within(menu).queryByRole("group", { name: "Pending organizations" })).toBeNull();
    expect(names(await openFlyout(menu, "Beta Labs"))).toEqual(["Acme", "Beta Labs"]);
  });
});

describe("CreateOrgDialog", () => {
  async function create(name: string) {
    fireEvent.change(await screen.findByLabelText("Organization name"), { target: { value: name } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
  }

  it("creates the org, then switches into exactly that org", async () => {
    renderDialog();
    expect(screen.getByRole("heading", { name: "Create organization" })).toBeInTheDocument();
    await create("  Northwind ");
    await waitFor(() => expect(navigated).toEqual(["/"]));
    expect(await posted("/api/v1/orgs")[0].json()).toEqual({ name: "Northwind" });
    expect(await posted("/api/v1/auth/refresh/org")[0].json()).toEqual({ org_team_id: ORG_N });
  });

  it("asks for a name and sends nothing without one", async () => {
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Name your organization")).toBeInTheDocument();
    expect(posted("/api/v1/orgs")).toHaveLength(0);
  });

  it.each([
    [
      "the creation limit",
      () => refusal(429, "org_creation_limited", "You've created too many organizations recently. Try again later."),
      "You've created too many organizations recently. Try again later.",
    ],
    [
      "a name the server refuses",
      () =>
        json(
          {
            error: {
              code: "validation_error",
              message: "The request failed validation.",
              trace_id: "t",
              details: { errors: [{ type: "value_error", loc: ["body", "name"], msg: "Value error, Organization name cannot contain a web address" }] },
            },
          },
          422,
        ),
      "Organization name cannot contain a web address",
    ],
    [
      "an unverified email",
      () => refusal(403, "email_verification_required", "Verify your email address to perform this action."),
      "Verify your email address to perform this action.",
    ],
    [
      "a session that is not a browser's",
      () => refusal(403, "browser_session_required", "Sign in to the portal to do this."),
      "Sign in to the portal to do this.",
    ],
  ])("says %s inline and switches nothing", async (_label, answer, sentence) => {
    createAnswer = answer;
    renderDialog();
    await create("Northwind");
    expect(await screen.findByText(sentence)).toBeInTheDocument();
    expect(posted("/api/v1/auth/refresh/org")).toHaveLength(0);
    expect(navigated).toEqual([]);
  });

  it("reads a 404 as not offered here", async () => {
    createAnswer = () => refusal(404, "not_found", "Not found");
    renderDialog();
    await create("Northwind");
    expect(await screen.findByText("Creating an organization isn't available.")).toBeInTheDocument();
    expect(posted("/api/v1/auth/refresh/org")).toHaveLength(0);
  });
});

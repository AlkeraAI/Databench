import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { AdminBansPage } from "@/pages/platform/admin/bans/AdminBansPage";
import { BAN_KEYS } from "@/pages/platform/admin/shared/confirm";

// The bans register driven end to end: the real hooks, the real MutationCache policy
// (so a ban's refresh is the one the product actually performs), only `fetch` stubbed.
// The cases pin what an operator sees and what the server is asked for — a wrong body,
// a swallowed refusal, or a lift that never re-reads the register all fail here.

const ACTIVE_USER_BAN = {
  id: "b-1",
  user_id: "u-1",
  user_email: "spam@farm.test",
  user_display_name: "Spam Farm",
  reason: "Account farming",
  created_at: "2026-09-01T10:00:00Z",
  created_by_id: "a-1",
  created_by_email: "ops@example.com",
  lifted_at: null,
  lifted_by_id: null,
  lifted_by_email: null,
  active: true,
};

const LIFTED_USER_BAN = {
  ...ACTIVE_USER_BAN,
  id: "b-0",
  user_id: "u-9",
  user_email: "reformed@example.com",
  user_display_name: "Reformed User",
  reason: "Mistake",
  lifted_at: "2026-09-02T09:00:00Z",
  lifted_by_id: "a-1",
  lifted_by_email: "ops@example.com",
  active: false,
};

const ACTIVE_DOMAIN_BAN = {
  id: "d-1",
  domain: "farm.test",
  reason: "Disposable mail",
  created_at: "2026-09-01T10:00:00Z",
  created_by_id: "a-1",
  created_by_email: "ops@example.com",
  lifted_at: null,
  lifted_by_id: null,
  lifted_by_email: null,
  active: true,
};

const ADMIN_USERS = [
  {
    id: "u-2",
    email: "ada@example.com",
    display_name: "Ada Lovelace",
    org_team_id: "t-1",
    org_name: "Northwind",
    platform_role: null,
    is_active: true,
    created_at: "2026-08-01T00:00:00Z",
    email_verified_at: "2026-08-01T01:00:00Z",
    disposable_email: false,
    signup_ip: null,
    last_login_ip: null,
    mtd_billed_nanos: 0,
    mtd_request_count: 0,
    banned: false,
    ban_reason: null,
  },
];

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** The register the stubbed backend currently holds. A test mutates these between
 *  calls so the refetch a mutation triggers returns the NEW state — the only way to
 *  prove the page re-reads rather than patching its own cache. */
let userBans: unknown[];
let domainBans: unknown[];
/** Per-test overrides for the write routes, keyed by `${method} ${pathFragment}`. */
let writes: Record<string, () => Response>;
let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  userBans = [ACTIVE_USER_BAN, LIFTED_USER_BAN];
  domainBans = [ACTIVE_DOMAIN_BAN];
  writes = {};
  fetchSpy = vi.fn((req: Request) => {
    const { url, method } = req;
    if (method === "GET" && url.includes("/admin/v1/bans/users")) return Promise.resolve(json(userBans));
    if (method === "GET" && url.includes("/admin/v1/bans/domains")) return Promise.resolve(json(domainBans));
    if (method === "GET" && url.includes("/admin/v1/users")) return Promise.resolve(json(ADMIN_USERS));
    for (const [key, make] of Object.entries(writes)) {
      const [wantMethod, fragment] = key.split(" ");
      if (method === wantMethod && url.includes(fragment)) return Promise.resolve(make());
    }
    return Promise.resolve(json({ error: { code: "not_found", message: `unrouted ${method} ${url}` } }, 404));
  });
  vi.stubGlobal("fetch", fetchSpy);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPage() {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <AdminBansPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** A register's card, scoped by its heading — the Card primitive has no landmark role. */
const card = (title: string): HTMLElement =>
  screen.getByRole("heading", { name: title }).closest(".alk-card") as HTMLElement;

/** The live (non-history) table of a register card. The history table lives inside the
 *  Collapse, which keeps its rows mounted while closed. */
const liveTable = (title: string): HTMLElement =>
  within(card(title)).getAllByRole("table")[0];

const historyRegion = (title: string): HTMLElement =>
  card(title).querySelector(".alk-collapse") as HTMLElement;

/** The key that answers the open confirmation. Scoped to the dialog on purpose: the domain
 *  form's own trigger carries the same verb, and only the one inside the dialog commits. */
const banKey = (label: string): HTMLElement =>
  within(screen.getByRole("dialog")).getByRole("button", { name: label });

const banCancel = (): HTMLElement =>
  within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" });

const writtenBodies = async (method: string, fragment: string) =>
  Promise.all(
    fetchSpy.mock.calls
      .map((c) => c[0] as Request)
      .filter((r) => r.method === method && r.url.includes(fragment))
      .map(async (r) => ({ url: r.url, body: await r.clone().text() })),
  );

describe("AdminBansPage", () => {
  it("lists the active bans in both registers", async () => {
    renderPage();
    expect(await screen.findByText("spam@farm.test")).toBeInTheDocument();
    expect(screen.getByText("Account farming")).toBeInTheDocument();
    expect(screen.getAllByText("ops@example.com").length).toBeGreaterThan(0);
    expect(screen.getByText("farm.test")).toBeInTheDocument();
    expect(screen.getByText("Disposable mail")).toBeInTheDocument();
  });

  it("keeps a lifted ban out of the live list and behind a closed disclosure", async () => {
    renderPage();
    await screen.findByText("spam@farm.test");

    // The live register carries only the active ban.
    expect(within(liveTable("Banned users")).getByText("spam@farm.test")).toBeInTheDocument();
    expect(within(liveTable("Banned users")).queryByText("reformed@example.com")).not.toBeInTheDocument();
    // The lifted one is the record, held closed until asked for.
    expect(historyRegion("Banned users")).toHaveAttribute("data-state", "closed");

    await userEvent.click(screen.getByRole("button", { name: "Lifted (1)" }));
    await waitFor(() => expect(historyRegion("Banned users")).toHaveAttribute("data-state", "open"));
    expect(within(historyRegion("Banned users")).getByText("reformed@example.com")).toBeInTheDocument();
    expect(within(historyRegion("Banned users")).getByText("ops@example.com")).toBeInTheDocument();
  });

  it("bans a picked user with the typed reason and shows the new row after the refresh", async () => {
    const created = { ...ACTIVE_USER_BAN, id: "b-2", user_id: "u-2", user_email: "ada@example.com", user_display_name: "Ada Lovelace", reason: "Abuse report" };
    writes["POST /admin/v1/bans/users"] = () => {
      userBans = [created, ...userBans];
      return json(created, 201);
    };
    renderPage();
    await screen.findByText("spam@farm.test");

    await userEvent.type(screen.getByLabelText("Search for a user to ban"), "ada");
    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await userEvent.type(within(card("Banned users")).getByLabelText("Reason (optional)"), "Abuse report");

    await userEvent.click(screen.getByRole("button", { name: "Ban user" }));
    // The confirmation names the target and says what a ban does.
    expect(await screen.findByText(/Ban ada@example.com\?/)).toBeInTheDocument();
    expect(screen.getByText(/signed out everywhere/i)).toBeInTheDocument();
    await userEvent.click(banKey(BAN_KEYS.account));

    const posts = await writtenBodies("POST", "/admin/v1/bans/users");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(posts[0].body)).toEqual({ user_id: "u-2", reason: "Abuse report" });
    expect(await screen.findByText("ada@example.com")).toBeInTheDocument();
  });

  // A ban signs the account out everywhere, so the register must not act on the press that
  // opens the question — only on the answer. Dismissing it has to leave the server untouched.
  it("backing out of the ban question sends nothing", async () => {
    renderPage();
    await screen.findByText("spam@farm.test");

    await userEvent.type(screen.getByLabelText("Search for a user to ban"), "ada");
    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await userEvent.click(screen.getByRole("button", { name: "Ban user" }));
    await screen.findByRole("dialog");
    await userEvent.click(banCancel());

    expect(await writtenBodies("POST", "/admin/v1/bans/users")).toHaveLength(0);
    // The picked user is still picked, so the operator can answer again without starting over.
    expect(screen.getByRole("button", { name: "Ban user" })).toBeInTheDocument();
  });

  it("a ban question opens on Cancel, and Enter does not ban", async () => {
    renderPage();
    await screen.findByText("spam@farm.test");

    await userEvent.type(screen.getByLabelText("Search for a user to ban"), "ada");
    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await userEvent.click(screen.getByRole("button", { name: "Ban user" }));
    await waitFor(() => expect(banCancel()).toHaveFocus());
    await userEvent.keyboard("{Enter}");

    expect(await writtenBodies("POST", "/admin/v1/bans/users")).toHaveLength(0);
  });

  it("shows the API's refusal verbatim when the account is already banned", async () => {
    writes["POST /admin/v1/bans/users"] = () =>
      json({ error: { code: "conflict", message: "This account is already banned." } }, 409);
    renderPage();
    await screen.findByText("spam@farm.test");

    await userEvent.type(screen.getByLabelText("Search for a user to ban"), "ada");
    await userEvent.click(await screen.findByRole("button", { name: /Ada Lovelace/ }));
    await userEvent.click(screen.getByRole("button", { name: "Ban user" }));
    await userEvent.click(banKey(BAN_KEYS.account));

    expect(await screen.findByText("This account is already banned.")).toBeInTheDocument();
  });

  it("lifts a user ban and the row moves into the history", async () => {
    writes["DELETE /admin/v1/bans/users/u-1"] = () => {
      userBans = [{ ...ACTIVE_USER_BAN, active: false, lifted_at: "2026-09-05T00:00:00Z", lifted_by_email: "ops@example.com" }, LIFTED_USER_BAN];
      return new Response(null, { status: 204 });
    };
    renderPage();
    await screen.findByText("spam@farm.test");

    await userEvent.click(within(liveTable("Banned users")).getByRole("button", { name: "Lift" }));

    const deletes = await writtenBodies("DELETE", "/admin/v1/bans/users/u-1");
    expect(deletes).toHaveLength(1);
    await waitFor(() =>
      expect(within(card("Banned users")).getByText("No accounts are banned.")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Lifted (2)" }));
    expect(within(historyRegion("Banned users")).getByText("spam@farm.test")).toBeInTheDocument();
  });

  it("refuses an empty domain field and otherwise sends the domain exactly as typed", async () => {
    const created = { ...ACTIVE_DOMAIN_BAN, id: "d-2", domain: "acme.com", reason: "" };
    writes["POST /admin/v1/bans/domains"] = () => {
      domainBans = [created, ...domainBans];
      return json(created, 201);
    };
    renderPage();
    await screen.findByText("farm.test");

    // Nothing typed: the action is unavailable rather than sending an empty ban.
    expect(screen.getByRole("button", { name: "Ban domain" })).toBeDisabled();

    // Typed with the shapes the SERVER normalizes — the client must not touch them.
    await userEvent.type(within(card("Banned domains")).getByLabelText("Domain"), " @ACME.com ");
    await userEvent.click(screen.getByRole("button", { name: "Ban domain" }));
    await userEvent.click(banKey(BAN_KEYS.domain));

    const posts = await writtenBodies("POST", "/admin/v1/bans/domains");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(posts[0].body)).toEqual({ domain: " @ACME.com ", reason: "" });
    expect(await screen.findByText("acme.com")).toBeInTheDocument();
  });

  it("shows the API's message when the domain is not a domain", async () => {
    writes["POST /admin/v1/bans/domains"] = () =>
      json(
        {
          error: {
            code: "validation_error",
            message: "The request failed validation.",
            details: { errors: [{ loc: ["body", "domain"], msg: "Value error, Enter a bare domain, e.g. acme.com" }] },
          },
        },
        422,
      );
    renderPage();
    await screen.findByText("farm.test");

    await userEvent.type(within(card("Banned domains")).getByLabelText("Domain"), "https://acme.com/signup");
    await userEvent.click(screen.getByRole("button", { name: "Ban domain" }));
    await userEvent.click(banKey(BAN_KEYS.domain));

    expect(await screen.findByText("Enter a bare domain, e.g. acme.com")).toBeInTheDocument();
  });

  it("lifts a domain ban through the domain path", async () => {
    writes["DELETE /admin/v1/bans/domains/farm.test"] = () => {
      domainBans = [{ ...ACTIVE_DOMAIN_BAN, active: false, lifted_at: "2026-09-05T00:00:00Z", lifted_by_email: "ops@example.com" }];
      return new Response(null, { status: 204 });
    };
    renderPage();
    await screen.findByText("farm.test");

    await userEvent.click(within(liveTable("Banned domains")).getByRole("button", { name: "Lift" }));

    expect(await writtenBodies("DELETE", "/admin/v1/bans/domains/farm.test")).toHaveLength(1);
    await waitFor(() =>
      expect(within(card("Banned domains")).getByText("No domains are banned.")).toBeInTheDocument(),
    );
  });
});

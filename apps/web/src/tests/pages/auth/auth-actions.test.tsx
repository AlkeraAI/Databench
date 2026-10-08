import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The hosted portal and the API live on different origins; the API's own pages
// (the Slack account link, Slack's install callback) must load from the API's.
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  apiBaseUrl: "https://api.example.test",
}));

import {
  GoTo,
  RealAuthActionsProvider,
  isServerPage,
  serverPageUrl,
  postAuthDestination,
  safeReturnTo,
  useAuthActions,
} from "@/pages/auth/auth-actions";
import { CARRIED_AUTH_PARAMS } from "@/app/extensions/portal";

// A parameter an extension carries between sign-in and sign-up, registered the way one
// does. The open pages carry none of their own.
CARRIED_AUTH_PARAMS.register({ key: "via", landing: (via) => `/welcome?via=${encodeURIComponent(via)}` });

// The post-auth navigation contract, owned by RealAuthActionsProvider: where the user
// lands after login / signup is decided by the auth page's own query string — a
// SANITIZED `?return_to=` deep link first, a parameter an extension carries next, home last. The
// sanitizer is the open-redirect guard, so its adversarial cases (absolute URL,
// protocol-relative, javascript:, backslash) are pinned directly, and the nastiest one
// is proven again through the real provider: a hostile return_to must land on "/".

describe("safeReturnTo", () => {
  it.each([
    ["/teams?tab=members", "/teams?tab=members"],
    ["/", "/"],
    ["/welcome?via=team", "/welcome?via=team"],
  ])("accepts the same-origin relative path %s", (path, expected) => {
    expect(safeReturnTo(path)).toBe(expected);
  });

  it.each([
    ["an absolute URL", "https://evil.com/phish"],
    ["a protocol-relative URL", "//evil.com"],
    ["a javascript: URL", "javascript:alert(1)"],
    ["a backslash path (browsers normalize \\ to /)", "/\\evil.com"],
    ["a relative path with no leading slash", "teams"],
    ["an empty string", ""],
  ])("rejects %s", (_label, path) => {
    expect(safeReturnTo(path)).toBeNull();
  });

  it("rejects null", () => {
    expect(safeReturnTo(null)).toBeNull();
  });

  // Same-origin, and still not a page: `/api/…` answers JSON and file bytes, so
  // a return_to naming it would hand a browser that has just signed in to a
  // download or a refusal where the product should be. The server decodes
  // percent-escapes before it routes, so the check has to as well.
  it.each([
    ["the API root", "/api"],
    ["an API route", "/api/v1/auth/me"],
    ["an API route with a query", "/api/v1/files/drives?limit=1"],
    ["another spelling of it", "/API/v1/auth/me"],
    ["a percent-escaped spelling the server still routes", "/%61pi/v1/auth/me"],
    ["a path that cannot be decoded at all", "/%E0%A4%A"],
  ])("rejects %s", (_label, path) => {
    expect(safeReturnTo(path)).toBeNull();
  });

  it.each([
    ["a page whose name merely starts with those letters", "/apiary"],
    ["a page with the word inside it", "/settings/api-keys"],
    ["a file the reader was sent a link to", "/files/nd_1"],
  ])("still accepts %s", (_label, path) => {
    expect(safeReturnTo(path)).toBe(path);
  });
});

// The API's own pages — the Slack account link and Slack's install callback —
// are where a signed-out browser is sent back to after signing in, so they are
// the one exception to "never return to /api". Exact paths only: a neighbour
// under the same prefix, or another casing, is still not a page.
describe("safeReturnTo — the API's own pages", () => {
  it.each([
    ["the Slack account link with its code", "/api/v1/slack/link?code=abc.def"],
    ["the Slack install callback with code and state", "/api/v1/slack/oauth/callback?code=c&state=s"],
    ["a percent-escaped spelling of the link", "/api/v1/slack/%6cink?code=abc"],
  ])("accepts %s", (_label, path) => {
    expect(safeReturnTo(path)).toBe(path);
    expect(isServerPage(path)).toBe(true);
  });

  it.each([
    ["the Slack events endpoint", "/api/v1/slack/events"],
    ["a path that only starts with the link's", "/api/v1/slack/link-other"],
    ["a sub-path of the link", "/api/v1/slack/link/extra"],
    ["another casing", "/API/v1/slack/link?code=abc"],
  ])("still rejects %s", (_label, path) => {
    expect(safeReturnTo(path)).toBeNull();
    expect(isServerPage(path)).toBe(false);
  });
});

describe("postAuthDestination", () => {
  it("prefers the sanitized return_to deep link", () => {
    expect(postAuthDestination("?return_to=%2Fteams%3Ftab%3Dmembers&via=team")).toBe("/teams?tab=members");
  });

  it("falls back to a carried parameter landing", () => {
    expect(postAuthDestination("?via=team")).toBe("/welcome?via=team");
  });

  it("lands home on a parameter no extension carries", () => {
    expect(postAuthDestination("?tier=team")).toBe("/");
  });

  it("falls through a hostile return_to to the next candidate", () => {
    expect(postAuthDestination("?return_to=//evil.com&via=team")).toBe("/welcome?via=team");
    expect(postAuthDestination("?return_to=https%3A%2F%2Fevil.com")).toBe("/");
  });

  it("defaults home with no params", () => {
    expect(postAuthDestination("")).toBe("/");
  });
});

// --- through the real provider ---------------------------------------------

const USER = {
  id: "u-1",
  first_name: "Ada",
  last_name: "Lovelace",
  email: "ada@acme.com",
  email_verification_required: false,
  has_password: true,
  created_at: "2026-01-01T00:00:00Z",
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

let fetchSpy: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchSpy = vi.fn(async (req: Request) => {
    if (req.url.includes("/auth/login")) return json(200, { user: USER, expires_at: "2026-12-01T00:00:00Z" });
    if (req.url.includes("/auth/signup")) return json(201, { user: USER, expires_at: "2026-12-01T00:00:00Z" });
    return json(200, {});
  });
  vi.stubGlobal("fetch", fetchSpy);
});
afterEach(() => vi.unstubAllGlobals());

/** Buttons that drive the seam the way the auth pages do, minus the form chrome. */
function Triggers() {
  const actions = useAuthActions();
  return (
    <>
      <button onClick={() => void actions.login({ email: "ada@acme.com", password: "pw" })}>do-login</button>
      <button onClick={() => void actions.signup({ email: "ada@acme.com", password: "longpassword" })}>
        do-signup
      </button>
      <button
        onClick={() =>
          void actions.signup({ email: "ada@gmail.com", password: "longpassword", allowPersonalEmail: true })
        }
      >
        do-signup-personal
      </button>
    </>
  );
}

/** Wherever the provider navigates, this echoes the landing path + query. */
function Probe() {
  const location = useLocation();
  return <div data-testid="landing">{location.pathname + location.search}</div>;
}

function renderAt(entry: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <RealAuthActionsProvider>
          <Routes>
            <Route path="/login" element={<Triggers />} />
            <Route path="*" element={<Probe />} />
          </Routes>
        </RealAuthActionsProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The stubbed request that hit a path, with its JSON body parsed (a Request, per openapi-fetch). */
async function requestTo(path: string): Promise<{ body: unknown }> {
  const req = fetchSpy.mock.calls.map((c) => c[0] as Request).find((r) => r.url.includes(path));
  if (!req) throw new Error(`no request to ${path}`);
  return { body: await req.clone().json().catch(() => undefined) };
}

describe("RealAuthActionsProvider — post-auth navigation", () => {
  it("honors ?return_to= after login (the guard's deep-link round-trip)", async () => {
    const user = userEvent.setup();
    renderAt(`/login?return_to=${encodeURIComponent("/teams?tab=members")}`);

    await user.click(screen.getByRole("button", { name: "do-login" }));

    expect(await screen.findByTestId("landing")).toHaveTextContent("/teams?tab=members");
  });

  it("lands home when the return_to is hostile (open-redirect guard)", async () => {
    const user = userEvent.setup();
    renderAt("/login?return_to=//evil.com");

    await user.click(screen.getByRole("button", { name: "do-login" }));

    expect(await screen.findByTestId("landing")).toHaveTextContent(/^\/$/);
  });

  it("lands where a carried parameter sends a login (?via=)", async () => {
    const user = userEvent.setup();
    renderAt("/login?via=team");

    await user.click(screen.getByRole("button", { name: "do-login" }));

    expect(await screen.findByTestId("landing")).toHaveTextContent("/welcome?via=team");
  });

  it("honors ?return_to= after signup too", async () => {
    const user = userEvent.setup();
    renderAt(`/login?return_to=${encodeURIComponent("/welcome?via=team")}`);

    await user.click(screen.getByRole("button", { name: "do-signup" }));

    expect(await screen.findByTestId("landing")).toHaveTextContent("/welcome?via=team");
  });
});

describe("RealAuthActionsProvider — a person in several orgs", () => {
  function answerLoginWith(extra: { choose_org: boolean; membership_count: number }) {
    fetchSpy.mockImplementation(async (req: Request) =>
      req.url.includes("/auth/login")
        ? json(200, {
            user: { ...USER, membership_count: extra.membership_count },
            expires_at: "2026-12-01T00:00:00Z",
            choose_org: extra.choose_org,
          })
        : json(200, {}),
    );
  }

  it("goes to the chooser, carrying the destination, when the server asks", async () => {
    answerLoginWith({ choose_org: true, membership_count: 2 });
    const user = userEvent.setup();
    renderAt(`/login?return_to=${encodeURIComponent("/files")}`);
    await user.click(screen.getByRole("button", { name: "do-login" }));
    expect(await screen.findByTestId("landing")).toHaveTextContent(
      `/choose-org?return_to=${encodeURIComponent("/files")}`,
    );
  });

  it("goes straight in to the last-used org otherwise, however many orgs there are", async () => {
    answerLoginWith({ choose_org: false, membership_count: 3 });
    const user = userEvent.setup();
    renderAt(`/login?return_to=${encodeURIComponent("/files")}`);
    await user.click(screen.getByRole("button", { name: "do-login" }));
    expect(await screen.findByTestId("landing")).toHaveTextContent(/^\/files$/);
  });
});

describe("RealAuthActionsProvider — signup wire payload", () => {
  it("sends allow_personal_email: false unless the user explicitly opted in", async () => {
    const user = userEvent.setup();
    renderAt("/login");

    await user.click(screen.getByRole("button", { name: "do-signup" }));

    await waitFor(async () => {
      const req = await requestTo("/auth/signup");
      expect(req.body).toMatchObject({ email: "ada@acme.com", allow_personal_email: false });
    });
  });

  it("sends allow_personal_email: true for the explicit opt-in", async () => {
    const user = userEvent.setup();
    renderAt("/login");

    await user.click(screen.getByRole("button", { name: "do-signup-personal" }));

    await waitFor(async () => {
      const req = await requestTo("/auth/signup");
      expect(req.body).toMatchObject({ email: "ada@gmail.com", allow_personal_email: true });
    });
  });
});

describe("RealAuthActionsProvider — returning to one of the API's own pages", () => {
  const replaced: string[] = [];
  const original = Object.getOwnPropertyDescriptor(window, "location");
  beforeEach(() => {
    replaced.length = 0;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, replace: (to: string) => replaced.push(to) },
    });
  });
  afterEach(() => {
    if (original) Object.defineProperty(window, "location", original);
  });

  it("loads the Slack account link as a page after sign-in, code intact", async () => {
    const user = userEvent.setup();
    const target = "/api/v1/slack/link?code=abc.def";
    renderAt(`/login?return_to=${encodeURIComponent(target)}`);

    await user.click(screen.getByRole("button", { name: "do-login" }));

    // On the API's origin: the portal host serves no /api and would 404.
    await waitFor(() => expect(replaced).toEqual([`https://api.example.test${target}`]));
    // The router never tried to render it (it would read "not found").
    expect(screen.queryByTestId("landing")).toBeNull();
  });

  it("keeps a portal return_to a router navigation", async () => {
    const user = userEvent.setup();
    renderAt(`/login?return_to=${encodeURIComponent("/teams")}`);

    await user.click(screen.getByRole("button", { name: "do-login" }));

    expect(await screen.findByTestId("landing")).toHaveTextContent("/teams");
    expect(replaced).toEqual([]);
  });

  it("GoTo loads a server page and routes a portal page", async () => {
    render(
      <MemoryRouter initialEntries={["/start"]}>
        <Routes>
          <Route path="/start" element={<GoTo to="/api/v1/slack/oauth/callback?code=c&state=s" />} />
          <Route path="*" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(replaced).toEqual(["https://api.example.test/api/v1/slack/oauth/callback?code=c&state=s"]),
    );
    expect(screen.queryByTestId("landing")).toBeNull();
  });
});

describe("serverPageUrl", () => {
  it("puts the path and query on the API origin", () => {
    expect(serverPageUrl("/api/v1/slack/link?code=a.b", "https://api.staging.example.com")).toBe(
      "https://api.staging.example.com/api/v1/slack/link?code=a.b",
    );
  });

  it("does not double the slash when the base ends in one", () => {
    expect(serverPageUrl("/api/v1/slack/link", "https://api.example.test/")).toBe(
      "https://api.example.test/api/v1/slack/link",
    );
  });

  it("stays same-origin when the API is the portal's own origin", () => {
    expect(serverPageUrl("/api/v1/slack/link?code=x", window.location.origin)).toBe(
      `${window.location.origin}/api/v1/slack/link?code=x`,
    );
  });
});

// --- several orgs per person: invitations carried through sign-in, no org left ---

describe("postAuthDestination with an invitation carried through sign-in", () => {
  it("returns to the invitation's landing, where a signed-in person accepts it", () => {
    expect(postAuthDestination("?invite=tok%2F1")).toBe("/signup?invite=tok%2F1");
  });

  it("still lets an explicit return_to win", () => {
    expect(postAuthDestination(`?invite=tok&return_to=${encodeURIComponent("/files")}`)).toBe("/files");
  });

  it("outranks a carried parameter", () => {
    expect(postAuthDestination("?invite=tok&via=team")).toBe("/signup?invite=tok");
  });
});

describe("RealAuthActionsProvider on a sign-in into no org", () => {
  function refuseLoginWith(status: number, body: unknown) {
    fetchSpy.mockImplementation(async (req: Request) =>
      req.url.includes("/auth/login") ? json(status, body) : json(200, {}),
    );
  }
  const NO_ORG = {
    error: { code: "no_active_membership", message: "You're not a member of any organization.", trace_id: "t" },
  };

  it("goes to the no-organization landing, keeping the invitation", async () => {
    refuseLoginWith(403, NO_ORG);
    const user = userEvent.setup();
    renderAt("/login?invite=tok-9");
    await user.click(screen.getByRole("button", { name: "do-login" }));
    expect(await screen.findByTestId("landing")).toHaveTextContent(/^\/no-organization\?invite=tok-9$/);
  });

  it("goes to the bare landing without an invitation", async () => {
    refuseLoginWith(403, NO_ORG);
    const user = userEvent.setup();
    renderAt("/login");
    await user.click(screen.getByRole("button", { name: "do-login" }));
    expect(await screen.findByTestId("landing")).toHaveTextContent(/^\/no-organization$/);
  });

  it("stays on sign-in for any other 403 (an org that wants its single sign-on)", async () => {
    refuseLoginWith(403, {
      error: { code: "sso_required", message: "Your organization requires single sign-on.", trace_id: "t" },
    });
    const errors: unknown[] = [];
    function CatchingLogin() {
      const actions = useAuthActions();
      return (
        <button onClick={() => void actions.login({ email: "a@b.co", password: "pw" }).catch((e) => errors.push(e))}>
          do-login
        </button>
      );
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/login?invite=tok-9"]}>
          <RealAuthActionsProvider>
            <Routes>
              <Route path="/login" element={<CatchingLogin />} />
              <Route path="*" element={<Probe />} />
            </Routes>
          </RealAuthActionsProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await userEvent.setup().click(screen.getByRole("button", { name: "do-login" }));
    await waitFor(() => expect(errors).toHaveLength(1));
    expect((errors[0] as { code: string }).code).toBe("sso_required");
    expect(screen.queryByTestId("landing")).toBeNull();
  });
});

describe("RealAuthActionsProvider on a signup for an address that has an account", () => {
  function Capture({ onError }: { onError: (e: unknown) => void }) {
    const actions = useAuthActions();
    return (
      <button
        onClick={() =>
          void actions
            .signup({ email: "ada@acme.com", password: "longpassword", inviteToken: "tok-3" })
            .catch(onError)
        }
      >
        do-signup
      </button>
    );
  }

  it("carries the refusal's code and the sign-in path it names", async () => {
    fetchSpy.mockImplementation(async (req: Request) =>
      req.url.includes("/auth/signup")
        ? json(409, {
            error: {
              code: "account_exists",
              message: "You already have an account. Sign in to continue.",
              trace_id: "t",
              details: { next: "/login?invite=tok-3" },
            },
          })
        : json(200, {}),
    );
    const errors: unknown[] = [];
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/signup?invite=tok-3"]}>
          <RealAuthActionsProvider>
            <Capture onError={(e) => errors.push(e)} />
          </RealAuthActionsProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await userEvent.setup().click(screen.getByRole("button", { name: "do-signup" }));
    await waitFor(() => expect(errors).toHaveLength(1));
    const error = errors[0] as { code: string; message: string; details: Record<string, unknown> };
    expect(error.code).toBe("account_exists");
    expect(error.message).toBe("You already have an account. Sign in to continue.");
    expect(error.details).toEqual({ next: "/login?invite=tok-3" });
  });
});

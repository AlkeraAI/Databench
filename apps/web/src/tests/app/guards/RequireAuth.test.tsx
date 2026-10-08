import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { meKey, useCurrentUser } from "@/api/auth";
import { createQueryClient, queryClient } from "@/api/queryClient";
import { AppContent } from "@/App";
import { RequireAuth } from "@/app/guards/RequireAuth";
import { LimitsProvider } from "@/lib/limits";

// HTTP status codes the guard's data contract turns on. 401 is the signed-out
// signal that MUST map to `null` (not an error); any other non-2xx MUST throw.
const HTTP_OK = 200;
const HTTP_UNAUTHORIZED = 401;
const HTTP_SERVER_ERROR = 500;

// The session guard, driven through the REAL useCurrentUser query against a REAL QueryClient —
// only `fetch` (the network boundary) is stubbed. We assert the observable contract, never class
// names: a 401 redirects the user to /login (the signed-out signal), a 200 user renders the
// protected children, and the loading gate holds (showing neither destination) while the session
// resolves. The status of /api/v1/auth/me is the single thing each render case varies.
//
// The guard renders the SAME thing (a /login redirect) whether the query resolves to `null` or
// throws, so the guard render alone cannot tell the two apart. The 401→null mapping (and its
// asymmetric counterpart — every OTHER non-2xx throws) is the data contract the guard depends on,
// and useCurrentUser is its only home, so two renderHook cases pin it directly below.

const meUser = {
  email: "admin@example.com",
  first_name: "Dana",
  last_name: "Dev Admin",
  id: "00000000-0000-0000-0000-000000000001",
  org_team_id: "00000000-0000-0000-0000-0000000000aa",
  display_name: "Dana Admin",
  email_verification_required: false,
  has_password: true,
  created_at: "2026-06-29T00:00:00Z",
};

function meResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// A fresh client per render with retries off — so the 401 case settles immediately instead of
// retrying, and no cache leaks between cases.
function renderGuard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<RequireAuth />}>
            <Route path="/" element={<div>protected workspace</div>} />
          </Route>
          <Route path="/login" element={<div>sign in to alkera</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RequireAuth", () => {
  it("redirects a signed-out visitor (401 from /me) to the login page", async () => {
    fetchSpy.mockResolvedValue(meResponse(HTTP_UNAUTHORIZED, { detail: "Not authenticated" }));
    renderGuard();

    expect(await screen.findByText("sign in to alkera")).toBeInTheDocument();
    expect(screen.queryByText("protected workspace")).not.toBeInTheDocument();
  });

  it("renders the protected children when /me returns the current user", async () => {
    fetchSpy.mockResolvedValue(meResponse(HTTP_OK, meUser));
    renderGuard();

    expect(await screen.findByText("protected workspace")).toBeInTheDocument();
    expect(screen.queryByText("sign in to alkera")).not.toBeInTheDocument();
  });

  it("sends a name-less account to /complete-profile before the app", async () => {
    // The state right after a minimal signup: authed but no name yet.
    fetchSpy.mockResolvedValue(
      meResponse(HTTP_OK, { ...meUser, first_name: "", last_name: "" }),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route element={<RequireAuth />}>
              <Route path="/" element={<div>protected workspace</div>} />
            </Route>
            <Route path="/complete-profile" element={<div>finish setting up</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("finish setting up")).toBeInTheDocument();
    expect(screen.queryByText("protected workspace")).not.toBeInTheDocument();
  });

  it("shows the loading gate (neither destination) until the session resolves", async () => {
    // A never-resolving fetch keeps the query pending so the gate's holding state is observable.
    fetchSpy.mockReturnValue(new Promise<Response>(() => {}));
    renderGuard();

    const gate = await screen.findByRole("status", { name: /loading your workspace/i });
    expect(gate).toBeInTheDocument();
    expect(screen.queryByText("protected workspace")).not.toBeInTheDocument();
    expect(screen.queryByText("sign in to alkera")).not.toBeInTheDocument();
  });

  it("redirects to /login carrying the attempted deep link as ?return_to=", async () => {
    fetchSpy.mockResolvedValue(meResponse(HTTP_UNAUTHORIZED, { detail: "Not authenticated" }));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // The login route echoes the `?return_to=` the guard handed it, so we can assert the
    // full path + query was preserved for the post-login return. A query param (not router
    // state) so the deep link survives a reload of the login page.
    function LoginProbe() {
      const returnTo = new URLSearchParams(useLocation().search).get("return_to") ?? "(none)";
      return <div data-testid="return-to">{returnTo}</div>;
    }
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/teams?tab=members"]}>
          <Routes>
            <Route element={<RequireAuth />}>
              <Route path="/teams" element={<div>teams</div>} />
            </Route>
            <Route path="/login" element={<LoginProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByTestId("return-to")).toHaveTextContent("/teams?tab=members");
    expect(screen.queryByText("teams")).not.toBeInTheDocument();
  });

  it("carries the attempted location to /complete-profile so the profile step returns there", async () => {
    // A signup headed for a page: the name-less account lands there, and the profile gate must
    // remember that destination — otherwise completing the profile strands the user at home.
    fetchSpy.mockResolvedValue(meResponse(HTTP_OK, { ...meUser, first_name: "", last_name: "" }));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    function ProfileProbe() {
      const returnTo = new URLSearchParams(useLocation().search).get("return_to") ?? "(none)";
      return <div data-testid="return-to">{returnTo}</div>;
    }
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/welcome?via=team"]}>
          <Routes>
            <Route element={<RequireAuth />}>
              <Route path="/welcome" element={<div>welcome</div>} />
            </Route>
            <Route path="/complete-profile" element={<ProfileProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByTestId("return-to")).toHaveTextContent("/welcome?via=team");
    expect(screen.queryByText("welcome")).not.toBeInTheDocument();
  });

  // The guard's data dependency. A 401 must be the benign signed-out signal (data === null,
  // NOT an error) — if it leaked through as an error, retry/error-boundary behavior elsewhere
  // would diverge; and a non-401 failure must surface as an error, never a silent `null` that
  // the guard would misread as "signed out". The guard render can't distinguish these, so we
  // pin them on the hook directly.
  function hookWrapper({ children }: { children: ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }

  it("maps a 401 to a `null` user without raising an error (the signed-out signal)", async () => {
    fetchSpy.mockResolvedValue(meResponse(HTTP_UNAUTHORIZED, { detail: "Not authenticated" }));
    const { result } = renderHook(() => useCurrentUser(), { wrapper: hookWrapper });

    await waitFor(() => expect(result.current.isPending).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.isError).toBe(false);
  });

  it("raises an error for a non-401 failure instead of silently signing the user out", async () => {
    fetchSpy.mockResolvedValue(meResponse(HTTP_SERVER_ERROR, { detail: "boom" }));
    const { result } = renderHook(() => useCurrentUser(), { wrapper: hookWrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));
    // Critically NOT `null` — a 500 mapped to null would let the guard treat a broken backend
    // as "signed out" and bounce an authed user to /login.
    expect(result.current.data).toBeUndefined();
  });
});

// A failure that is NOT an answer.
//
// The session read is the one query whose failure the whole shell turns on, and for a while any
// failure of it read as "signed out": a 429 from the rate limiter, a 500 from a restarting API or a
// dropped connection all put a full Sign-in card in front of somebody whose cookie was still valid,
// who then retyped their password into a rate limiter. Only a 401 — after the transport's own
// refresh has had its go — is an answer. Everything else is a failure to ask, and the guard has to
// say so without throwing the session away.
//
// The limits provider drops the retry wait to zero so these cases exercise the real ladder in a
// test's lifetime rather than a fixed-delay one nobody would wait for.
describe("a session read that fails without answering", () => {
  const RETRY_LIMITS = { sessionRetryFloorMs: 0, sessionRetryAttempts: 2 };

  function renderShell(entries = ["/"]) {
    const qc = createQueryClient();
    return render(
      <QueryClientProvider client={qc}>
        <LimitsProvider overrides={RETRY_LIMITS}>
          <MemoryRouter initialEntries={entries}>
            <Routes>
              <Route element={<RequireAuth />}>
                <Route path="/" element={<div>protected workspace</div>} />
                <Route path="/teams" element={<div>protected workspace</div>} />
              </Route>
              <Route path="/login" element={<div>sign in to alkera</div>} />
            </Routes>
          </MemoryRouter>
        </LimitsProvider>
      </QueryClientProvider>,
    );
  }

  it.each([
    {
      what: "a rate limit",
      reply: () => meResponse(429, { detail: "Too many requests" }),
    },
    { what: "a server error", reply: () => meResponse(HTTP_SERVER_ERROR, { detail: "boom" }) },
    { what: "a gateway error", reply: () => meResponse(502, { detail: "bad gateway" }) },
  ])("$what keeps the reader out of the sign-in page", async ({ reply }) => {
    fetchSpy.mockImplementation(async () => reply());
    renderShell();

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load your session");
    expect(screen.queryByText("sign in to alkera")).not.toBeInTheDocument();
    expect(screen.queryByText("protected workspace")).not.toBeInTheDocument();
  });

  it("a dropped connection does the same", async () => {
    fetchSpy.mockRejectedValue(new TypeError("Failed to fetch"));
    renderShell();

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load your session");
    expect(screen.queryByText("sign in to alkera")).not.toBeInTheDocument();
  });

  it("a 401 still means signed out", async () => {
    fetchSpy.mockResolvedValue(meResponse(HTTP_UNAUTHORIZED, { detail: "Not authenticated" }));
    renderShell();

    expect(await screen.findByText("sign in to alkera")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each([
    { status: 429, says: "The server is limiting how often it will answer." },
    // The plate's default sentence is about the request not arriving, which for a 5xx is simply
    // false: it arrived and the server answered badly.
    { status: HTTP_SERVER_ERROR, says: "The server could not answer." },
    { status: 503, says: "The server could not answer." },
  ])("describes a $status as what actually happened", async ({ status, says }) => {
    fetchSpy.mockImplementation(async () => meResponse(status, { detail: "x" }));
    renderShell();

    expect(await screen.findByRole("alert")).toHaveTextContent(says);
    expect(screen.getByRole("alert")).not.toHaveTextContent("did not reach the server");
  });

  it("falls back to the transport sentence when nothing was served at all", async () => {
    fetchSpy.mockRejectedValue(new TypeError("Failed to fetch"));
    renderShell();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The request did not reach the server.",
    );
  });

  it("waits the length the server named before asking again", async () => {
    // The refusal above the cap is pinned below, but the DELAY itself was not: deleting the
    // `retryDelay` wiring left every case green while a rate-limited tab went back on the library's
    // own one-second ladder instead of the wait the server named. The asked-for wait is two
    // seconds precisely so that ladder cannot be mistaken for it.
    const at: number[] = [];
    fetchSpy.mockImplementation(async () => {
      at.push(Date.now());
      return new Response(JSON.stringify({ detail: "Too many requests" }), {
        status: 429,
        headers: { "content-type": "application/json", "retry-after": "2" },
      });
    });
    const qc = createQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <LimitsProvider overrides={{ sessionRetryFloorMs: 0, sessionRetryAttempts: 1 }}>
          <MemoryRouter initialEntries={["/"]}>
            <Routes>
              <Route element={<RequireAuth />}>
                <Route path="/" element={<div>protected workspace</div>} />
              </Route>
              <Route path="/login" element={<div>sign in to alkera</div>} />
            </Routes>
          </MemoryRouter>
        </LimitsProvider>
      </QueryClientProvider>,
    );
    await screen.findByRole("alert", {}, { timeout: 8000 });

    expect(at).toHaveLength(2);
    // The floor is zero and the library's own first step is one second, so a gap past 1.5 s can
    // only be the header, and one under it can only be something other than the header.
    const gap = (at[1] ?? 0) - (at[0] ?? 0);
    expect(gap).toBeGreaterThan(1500);
    expect(gap).toBeLessThan(4000);
  });

  it("repeats what the server asked the reader to wait", async () => {
    fetchSpy.mockImplementation(
      async () =>
        new Response(JSON.stringify({ detail: "Too many requests" }), {
          status: 429,
          headers: { "content-type": "application/json", "retry-after": "45" },
        }),
    );
    renderShell();

    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 45 seconds.");
  });

  it("retries the read on demand and lets the reader through when it answers", async () => {
    fetchSpy.mockImplementation(async () => meResponse(HTTP_SERVER_ERROR, { detail: "boom" }));
    renderShell();
    await screen.findByRole("alert");

    fetchSpy.mockImplementation(async () => meResponse(HTTP_OK, meUser));
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("protected workspace")).toBeInTheDocument();
  });

  it("does not sign out a reader whose session is already known", async () => {
    // The read succeeded once and then failed: the session that answered is still the truth, so
    // the app stays up and the failure is a notice rather than a gate.
    const qc = createQueryClient();
    // Stale on arrival, so the mount re-asks the question the stub is about to fail.
    qc.setQueryData(meKey, meUser, { updatedAt: Date.now() - 60_000 });
    fetchSpy.mockImplementation(async () => meResponse(HTTP_SERVER_ERROR, { detail: "boom" }));
    render(
      <QueryClientProvider client={qc}>
        <LimitsProvider overrides={RETRY_LIMITS}>
          <MemoryRouter initialEntries={["/"]}>
            <Routes>
              <Route element={<RequireAuth />}>
                <Route path="/" element={<div>protected workspace</div>} />
              </Route>
              <Route path="/login" element={<div>sign in to alkera</div>} />
            </Routes>
          </MemoryRouter>
        </LimitsProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(qc.getQueryState(meKey)?.status).toBe("error"));

    expect(screen.getByText("protected workspace")).toBeInTheDocument();
    expect(screen.queryByText("sign in to alkera")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Could not check your session.");
  });

  it("asks again before giving up, and never re-asks an answer", async () => {
    fetchSpy.mockImplementation(async () => meResponse(HTTP_SERVER_ERROR, { detail: "boom" }));
    renderShell();
    await screen.findByRole("alert");
    // The ladder: the first ask plus the two the limits allow.
    const transient = fetchSpy.mock.calls.length;
    expect(transient).toBe(3);

    fetchSpy.mockClear();
    fetchSpy.mockResolvedValue(meResponse(HTTP_UNAUTHORIZED, { detail: "Not authenticated" }));
    renderShell();
    await screen.findByText("sign in to alkera");
    expect(fetchSpy.mock.calls.length).toBe(1);
  });
});

// A link to one file in the drive, opened by somebody who is not signed in.
//
// Through the REAL route table, because the thing being pinned is where the
// Files routes SIT: outside the guard they would mount and start reading the
// drive with no session, which is a burst of refusals on every shared link — and
// the deep link the reader followed has to survive the login, or they arrive at
// a dashboard with no idea what they clicked.
describe("a Files deep link followed by a signed-out visitor", () => {
  function Where() {
    const location = useLocation();
    return <span data-testid="where">{`${location.pathname}${location.search}`}</span>;
  }

  const renderApp = (path: string) =>
    render(
      <MemoryRouter initialEntries={[path]}>
        <AppContent />
        <Where />
      </MemoryRouter>,
    );

  beforeEach(() => queryClient.clear());
  afterEach(() => queryClient.clear());

  it("carries the file's address through to the login page", async () => {
    fetchSpy.mockImplementation(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/v1/config")) return meResponse(HTTP_OK, { self_hosted: false });
      return meResponse(HTTP_UNAUTHORIZED, { detail: "Not authenticated" });
    });
    renderApp("/files/nd_1");

    await waitFor(() =>
      expect(screen.getByTestId("where")).toHaveTextContent("/login?return_to=%2Ffiles%2Fnd_1"),
    );
  });

  it("reads nothing of the drive before the session has answered", async () => {
    // The gate holds while /me is in flight. Anything the drive asks for here is
    // asked with no session and can only be refused.
    fetchSpy.mockImplementation(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/v1/config")) return meResponse(HTTP_OK, { self_hosted: false });
      if (url.includes("/api/v1/auth/me")) return new Promise<Response>(() => {});
      return meResponse(HTTP_OK, {});
    });
    renderApp("/files/nd_1");

    await screen.findByRole("status", { name: /loading your workspace/i });
    await new Promise((resolve) => setTimeout(resolve, 50));
    const asked = fetchSpy.mock.calls.map(([input]) =>
      input instanceof Request ? input.url : String(input),
    );
    expect(asked.filter((url) => url.includes("/api/v1/files/"))).toEqual([]);
  });
});

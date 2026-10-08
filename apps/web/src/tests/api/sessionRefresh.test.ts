// The silent refresh behind the API client. The access token lives minutes and the
// HTTP-only refresh cookie keeps the session: a `token_expired` 401 is refreshed
// ONCE (a single in-flight refresh shared by every concurrent request) and the
// request retried once; a refused refresh hands the user to login; and a token
// within two minutes of its `exp` is refreshed before the next request goes out.
// Every case drives the real `api` client with `fetch` stubbed.

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import {
  PROACTIVE_REFRESH_MS,
  RETRY_REFRESH_MS,
  api,
  noteSessionExpiry,
  refreshSession,
  setUnauthorizedHandler,
} from "@/api/client";

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const expired = () => json({ error: { code: "token_expired", message: "session token expired" } }, 401);
const revoked = () => json({ error: { code: "session_revoked", message: "session revoked" } }, 401);

let calls: string[];
let unauthorized: Mock<() => void>;
let refreshAnswers: (() => Response)[];
let refreshed: number;

function pathOf(input: Request | string): string {
  return new URL(typeof input === "string" ? input : input.url).pathname;
}

beforeEach(() => {
  calls = [];
  refreshed = 0;
  refreshAnswers = [];
  unauthorized = vi.fn();
  setUnauthorizedHandler(unauthorized);
  noteSessionExpiry(null);
});

afterEach(() => {
  setUnauthorizedHandler(null);
  noteSessionExpiry(null);
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/** A server whose `/auth/me` answers `token_expired` until a refresh has landed. */
function serverThatExpiresUntilRefreshed(opts: { refreshOk: boolean }) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string) => {
      const p = pathOf(input);
      calls.push(p);
      if (p === "/api/v1/auth/refresh") {
        const scripted = refreshAnswers.shift();
        if (scripted) return scripted();
        if (!opts.refreshOk) return revoked();
        refreshed += 1;
        return json({ user: { id: "u" }, expires_at: new Date(Date.now() + 1_800_000).toISOString() });
      }
      if (p === "/api/v1/auth/me") return refreshed > 0 ? json({ id: "u", email: "a@b.c" }) : expired();
      return json({ detail: `unmatched ${p}` }, 404);
    }),
  );
}

describe("silent refresh", () => {
  it("refreshes once on token_expired and retries the request once", async () => {
    serverThatExpiresUntilRefreshed({ refreshOk: true });
    const result = await api.GET("/api/v1/auth/me");
    expect(result.response.status).toBe(200);
    expect(calls).toEqual(["/api/v1/auth/me", "/api/v1/auth/refresh", "/api/v1/auth/me"]);
    expect(unauthorized).not.toHaveBeenCalled();
  });

  it("shares ONE in-flight refresh across concurrent token_expired answers", async () => {
    serverThatExpiresUntilRefreshed({ refreshOk: true });
    const results = await Promise.all([
      api.GET("/api/v1/auth/me"),
      api.GET("/api/v1/auth/me"),
      api.GET("/api/v1/auth/me"),
    ]);
    expect(results.map((r) => r.response.status)).toEqual([200, 200, 200]);
    expect(calls.filter((p) => p === "/api/v1/auth/refresh")).toHaveLength(1);
    expect(unauthorized).not.toHaveBeenCalled();
  });

  it("does not retry more than once: a second token_expired after a refresh stands", async () => {
    // The refresh "succeeds" but the server keeps saying expired — a retry loop
    // would spin; the contract is exactly one refresh and one retry per request.
    serverThatExpiresUntilRefreshed({ refreshOk: true });
    refreshAnswers.push(() => json({ user: { id: "u" }, expires_at: null }));
    const result = await api.GET("/api/v1/auth/me");
    expect(result.response.status).toBe(401);
    expect(calls).toEqual(["/api/v1/auth/me", "/api/v1/auth/refresh", "/api/v1/auth/me"]);
  });

  it("fires the unauthorized reaction when the refresh is refused", async () => {
    serverThatExpiresUntilRefreshed({ refreshOk: false });
    const result = await api.GET("/api/v1/auth/me");
    expect(result.response.status).toBe(401);
    expect(calls).toEqual(["/api/v1/auth/me", "/api/v1/auth/refresh"]);
    expect(unauthorized).toHaveBeenCalledTimes(1);
  });

  it("treats a refresh that fails for any reason but 401 as transient, not a sign-out", async () => {
    serverThatExpiresUntilRefreshed({ refreshOk: true });
    refreshAnswers.push(() => json({ error: { code: "unavailable", message: "later" } }, 503));
    const result = await api.GET("/api/v1/auth/me");
    expect(result.response.status).toBe(401);
    expect(calls).toEqual(["/api/v1/auth/me", "/api/v1/auth/refresh"]);
    expect(unauthorized).not.toHaveBeenCalled();
  });

  it("never refreshes on a revoked session or any other 401", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: Request | string) => {
        calls.push(pathOf(input));
        return revoked();
      }),
    );
    const result = await api.GET("/api/v1/teams");
    expect(result.response.status).toBe(401);
    expect(calls).toEqual(["/api/v1/teams"]);
    expect(unauthorized).toHaveBeenCalledTimes(1);
  });

  it("refreshes proactively before a request when the access token is within two minutes of exp", async () => {
    vi.useFakeTimers();
    serverThatExpiresUntilRefreshed({ refreshOk: true });
    noteSessionExpiry(new Date(Date.now() + PROACTIVE_REFRESH_MS + 30_000).toISOString());
    // Well before the window: no refresh rides ahead of the request.
    vi.setSystemTime(Date.now() + 10_000);
    refreshed = 1; // pretend a valid token so /auth/me answers 200
    await api.GET("/api/v1/auth/me");
    expect(calls).toEqual(["/api/v1/auth/me"]);
    // Inside the window: the refresh goes out FIRST, then the request.
    calls = [];
    vi.setSystemTime(Date.now() + 30_000);
    await api.GET("/api/v1/auth/me");
    expect(calls).toEqual(["/api/v1/auth/refresh", "/api/v1/auth/me"]);
  });

  it("arms a timer that refreshes an idle tab two minutes before exp", async () => {
    vi.useFakeTimers();
    serverThatExpiresUntilRefreshed({ refreshOk: true });
    noteSessionExpiry(new Date(Date.now() + PROACTIVE_REFRESH_MS + 60_000).toISOString());
    await vi.advanceTimersByTimeAsync(59_000);
    expect(calls).toEqual([]);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(calls).toEqual(["/api/v1/auth/refresh"]);
  });

  it("refreshSession is single-flight for direct callers too", async () => {
    serverThatExpiresUntilRefreshed({ refreshOk: true });
    const [a, b] = await Promise.all([refreshSession(), refreshSession()]);
    expect([a, b]).toEqual([true, true]);
    expect(calls).toEqual(["/api/v1/auth/refresh"]);
  });
});

// A refresh that fails for a reason that is not a refusal — a 5xx, the refresh route's own
// throttle, a dropped connection — must leave the session on a path back to renewal. The
// timer that fired has already cleared itself, so without re-arming it the only remaining
// trigger is a request going out through `api`; the transports that do not go through it
// (the event stream reconnecting on the server's deadline) would then be the first thing to
// meet a lapsed cookie, and the person is signed out by something they never clicked.
describe("a transient refresh failure keeps trying inside the window", () => {
  it("re-arms after a 5xx and renews on the next attempt", async () => {
    vi.useFakeTimers();
    serverThatExpiresUntilRefreshed({ refreshOk: true });
    refreshAnswers.push(() => json({ detail: "upstream" }, 503));
    noteSessionExpiry(new Date(Date.now() + PROACTIVE_REFRESH_MS + 60_000).toISOString());

    await vi.advanceTimersByTimeAsync(61_000);
    expect(calls).toEqual(["/api/v1/auth/refresh"]);
    expect(refreshed).toBe(0);

    await vi.advanceTimersByTimeAsync(RETRY_REFRESH_MS);
    expect(calls).toEqual(["/api/v1/auth/refresh", "/api/v1/auth/refresh"]);
    expect(refreshed).toBe(1);
    expect(unauthorized).not.toHaveBeenCalled();
  });

  it("re-arms after the refresh route's own throttle answers 429", async () => {
    vi.useFakeTimers();
    serverThatExpiresUntilRefreshed({ refreshOk: true });
    refreshAnswers.push(() => json({ detail: "slow down" }, 429));
    noteSessionExpiry(new Date(Date.now() + PROACTIVE_REFRESH_MS + 60_000).toISOString());

    await vi.advanceTimersByTimeAsync(61_000 + RETRY_REFRESH_MS);
    expect(refreshed).toBe(1);
  });

  it("re-arms after a network failure", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: Request | string) => {
        const p = pathOf(input);
        calls.push(p);
        if (calls.length === 1) throw new TypeError("network down");
        refreshed += 1;
        return json({ expires_at: new Date(Date.now() + 1_800_000).toISOString() });
      }),
    );
    noteSessionExpiry(new Date(Date.now() + PROACTIVE_REFRESH_MS + 60_000).toISOString());

    await vi.advanceTimersByTimeAsync(61_000 + RETRY_REFRESH_MS);
    expect(refreshed).toBe(1);
    expect(unauthorized).not.toHaveBeenCalled();
  });

  it("stops re-arming once the access token has lapsed — the next request's 401 is the trigger", async () => {
    vi.useFakeTimers();
    serverThatExpiresUntilRefreshed({ refreshOk: true });
    refreshAnswers.push(() => json({ detail: "upstream" }, 503));
    // Two minutes of life left, so the proactive timer fires at once and the retry would land
    // past `exp`.
    noteSessionExpiry(new Date(Date.now() + PROACTIVE_REFRESH_MS).toISOString());
    await vi.advanceTimersByTimeAsync(1);
    expect(calls).toEqual(["/api/v1/auth/refresh"]);

    vi.setSystemTime(Date.now() + PROACTIVE_REFRESH_MS + 1_000);
    await vi.advanceTimersByTimeAsync(10 * RETRY_REFRESH_MS);
    expect(calls).toEqual(["/api/v1/auth/refresh"]);
  });

  it("a refusal is not transient: it forgets the session and bounces, with nothing re-armed", async () => {
    vi.useFakeTimers();
    serverThatExpiresUntilRefreshed({ refreshOk: false });
    noteSessionExpiry(new Date(Date.now() + PROACTIVE_REFRESH_MS + 60_000).toISOString());

    await vi.advanceTimersByTimeAsync(61_000);
    expect(unauthorized).toHaveBeenCalledTimes(1);
    calls = [];
    await vi.advanceTimersByTimeAsync(10 * RETRY_REFRESH_MS);
    expect(calls).toEqual([]);
  });
});

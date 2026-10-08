// The bridge from the server event stream to the query cache, driven end to end through a
// real `createQueryClient` (so invalidation is the shared policy's prefix match) with the
// stream's `fetch` scripted: it opens only for a signed-in user, turns a frame into exactly
// the invalidations the event map names, turns a `reset` and a resume-after-down into an
// invalidate-everything, answers a 401 with the same one silent refresh the API client makes
// and only then hands it to the shared reaction (the cached identity is dropped, so the guards
// bounce to login), and mirrors its status into the store.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ORG_HEADER, enterOrg, forgetActiveOrg } from "@/api/activeOrg";
import type { CurrentUser } from "@/api/auth";
import { RealtimeBridge } from "@/api/events/RealtimeBridge";
import { resetRealtimeStatus, useRealtimeStatus } from "@/api/events/status";
import { keys } from "@/api/keys";
import { apiBaseUrl, noteSessionExpiry } from "@/api/client";
import { createQueryClient } from "@/api/queryClient";
import { UnauthorizedBridge } from "@/app/boot/UnauthorizedBridge";

import { advance, openStream, scriptedFetch } from "./fakeSse";

const USER = {
  id: "11111111-1111-1111-1111-111111111111",
  email: "ada@example.com",
  first_name: "Ada",
  last_name: "Lovelace",
  display_name: "Ada Lovelace",
  org_team_id: "22222222-2222-2222-2222-222222222222",
  org_name: "Northwind Labs",
  org_role: "member",
  membership_count: 1,
  has_password: true,
  mfa_enabled: false,
  platform_role: null,
  platform_role_display: null,
  email_verified_at: "2026-07-30T00:00:00Z",
  email_verification_required: false,
  email_verification_deadline: null,
  verification_resend_available_at: null,
  created_at: "2026-07-30T00:00:00Z",
} satisfies CurrentUser;

const ORG = USER.org_team_id;
const RUN = "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f";

const frame = (id: number, type: string, entity_id: string): string =>
  `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify({ type, entity: "e", entity_id, version: 1, org_id: ORG })}\n\n`;

let script: ReturnType<typeof scriptedFetch>;

beforeEach(() => {
  vi.useFakeTimers();
  // The bridge builds its client with the production backoff; pin its jitter so the schedule
  // below is exact (floor 2 s, then 4 s, then 8 s).
  vi.spyOn(Math, "random").mockReturnValue(0);
  resetRealtimeStatus();
  script = scriptedFetch();
  // The identity query is seeded fresh, so `useCurrentUser` never reaches the network here;
  // a global stub keeps any stray request deterministic all the same.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(USER), { status: 200, headers: { "content-type": "application/json" } })),
  );
});

afterEach(() => {
  cleanup();
  // The refresh expiry is module state on the API client: left set, its proactive timer would
  // fire into the next test's fake clock.
  noteSessionExpiry(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

/** Answer the app's own `POST /auth/refresh` — the one request the bridge makes off the
 *  scripted stream `fetch`, because the silent refresh goes through the global one. */
function stubRefresh(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        }),
    ),
  );
}

function mount(user: CurrentUser | null) {
  const qc = createQueryClient({ retry: false });
  qc.setQueryData(keys.auth.me, user);
  const view = render(
    <QueryClientProvider client={qc}>
      <UnauthorizedBridge />
      <RealtimeBridge fetch={script.fetch} />
    </QueryClientProvider>,
  );
  return { qc, ...view };
}

describe("RealtimeBridge", () => {
  it("opens the stream with the session cookie when signed in", async () => {
    script.answerStream(openStream());
    mount(USER);
    await advance(0);
    expect(script.requests).toHaveLength(1);
    expect(script.requests[0].url).toBe(`${apiBaseUrl}/api/v1/events`);
    expect(script.requests[0].credentials).toBe("include");
    expect(useRealtimeStatus.getState().sse).toBe("connected");
  });

  it("names the org the tab rendered on the stream, so a stale tab cannot listen to another org", async () => {
    enterOrg(USER.org_team_id);
    script.answerStream(openStream());
    mount(USER);
    await advance(0);
    expect(script.requests[0].headers[ORG_HEADER.toLowerCase()]).toBe(USER.org_team_id);
    forgetActiveOrg();
  });

  it("opens nothing when signed out and leaves the stream idle", async () => {
    mount(null);
    await advance(1_000);
    expect(script.requests).toHaveLength(0);
    expect(useRealtimeStatus.getState().sse).toBe("idle");
  });

  it("a gate_run.ingested frame invalidates the run, the history, the live band, the status and the drift feed — and nothing else", async () => {
    const stream = openStream();
    script.answerStream(stream);
    const { qc } = mount(USER);
    qc.setQueryData(keys.gate.run(RUN), { id: RUN });
    qc.setQueryData(keys.gate.runs(), []);
    qc.setQueryData(keys.gate.activity(), {});
    qc.setQueryData(keys.gate.status, {});
    qc.setQueryData(keys.gate.drift(), []);
    qc.setQueryData(keys.billing.summary, {});
    qc.setQueryData(keys.kb.catalog, {});
    await advance(0);

    stream.push(frame(1, "gate_run.ingested", RUN));
    await advance(250);

    for (const key of [keys.gate.run(RUN), keys.gate.runs(), keys.gate.activity(), keys.gate.status, keys.gate.drift()]) {
      expect(qc.getQueryState(key)?.isInvalidated, JSON.stringify(key)).toBe(true);
    }
    expect(qc.getQueryState(keys.billing.summary)?.isInvalidated).toBe(false);
    expect(qc.getQueryState(keys.kb.catalog)?.isInvalidated).toBe(false);
    // The identity is not among the run's slots either.
    expect(qc.getQueryData(keys.auth.me)).toEqual(USER);
  });

  it("a burst of frames within the window is one invalidation pass", async () => {
    const stream = openStream();
    script.answerStream(stream);
    const { qc } = mount(USER);
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    await advance(0);
    for (let i = 0; i < 20; i += 1) stream.push(frame(i + 1, "kb_item.changed", `k${i}`));
    await advance(249);
    expect(invalidate).not.toHaveBeenCalled();
    await advance(1);
    // One key, flushed as a named pass and a prefix pass.
    expect(invalidate).toHaveBeenCalledTimes(2);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: keys.kb.all, exact: true });
  });

  it("a reset frame invalidates every query", async () => {
    const stream = openStream();
    script.answerStream(stream);
    const { qc } = mount(USER);
    qc.setQueryData(keys.billing.summary, {});
    qc.setQueryData(keys.kb.catalog, {});
    await advance(0);

    stream.push('event: reset\ndata: {"reason":"overflow"}\n\n');
    await advance(250);
    expect(qc.getQueryState(keys.billing.summary)?.isInvalidated).toBe(true);
    expect(qc.getQueryState(keys.kb.catalog)?.isInvalidated).toBe(true);
  });

  it("a frame from a newer server (an unknown type) is ignored, not thrown on", async () => {
    const stream = openStream();
    script.answerStream(stream);
    const { qc } = mount(USER);
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    await advance(0);
    stream.push(frame(1, "kb_item.renamed", RUN));
    stream.push("id: 2\nevent: kb_item.changed\ndata: {not json}\n\n");
    await advance(250);
    expect(invalidate).not.toHaveBeenCalled();
    expect(useRealtimeStatus.getState().sse).toBe("connected");
  });

  it("a 401 on the stream is refreshed once, and a renewed session keeps the user signed in", async () => {
    // The stream reconnects on the server's own deadline, long after the last click, so it is
    // the first thing to meet a lapsed access cookie. Renewing it keeps the session from
    // ending at the reconnect.
    stubRefresh(200, { expires_at: "2099-01-01T00:00:00Z" });
    script.answerStatus(401);
    script.answerStream(openStream());
    const { qc } = mount(USER);
    await act(async () => {
      await advance(0);
    });
    expect(qc.getQueryData(keys.auth.me)).toEqual(USER);
    expect(script.requests).toHaveLength(2);
  });

  it("a 401 the refresh cannot fix drops the cached identity through the shared unauthorized reaction", async () => {
    stubRefresh(401, { error: { code: "session_revoked", message: "session revoked" } });
    script.answerStatus(401);
    const { qc } = mount(USER);
    // The identity flip re-renders the bridge's own observer, hence the act.
    await act(async () => {
      await advance(0);
    });
    expect(qc.getQueryData(keys.auth.me)).toBeNull();
    // Signed out now: the bridge closes its client and opens nothing further.
    await advance(120_000);
    expect(script.requests).toHaveLength(1);
    expect(useRealtimeStatus.getState().sse).toBe("idle");
  });

  it("mirrors the client's status into the store: connecting, connected, reconnecting, down", async () => {
    const stream = openStream();
    script.answerStream(stream);
    script.answerThrow();
    script.answerThrow();
    const seen: string[] = [];
    useRealtimeStatus.subscribe((s) => seen.push(s.sse));
    mount(USER);
    await advance(0);
    stream.fail();
    await advance(0);
    expect(useRealtimeStatus.getState().sse).toBe("reconnecting");
    await advance(5_000);
    expect(useRealtimeStatus.getState().sse).toBe("down");
    expect(seen).toEqual(["connecting", "connected", "reconnecting", "down"]);
  });

  it("a stream that comes back after having been down invalidates everything exactly once", async () => {
    script.answerThrow();
    script.answerThrow();
    script.answerThrow();
    const stream = openStream();
    script.answerStream(stream);
    const { qc } = mount(USER);
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    await advance(6_000); // attempts at 0 s, 2 s, 6 s; down at 5 s
    expect(useRealtimeStatus.getState().sse).toBe("down");
    await advance(8_000); // the fourth attempt (2+4+8 = 14 s) connects
    expect(useRealtimeStatus.getState().sse).toBe("connected");
    await advance(250);
    expect(invalidate).toHaveBeenCalledTimes(1);
    // With no carve-out: the gap is unknown, so nothing is assumed still gone.
    expect(invalidate).toHaveBeenCalledWith();
  });

  it("a brief reconnect that never reached down does not invalidate everything (the cursor replays the gap)", async () => {
    const first = openStream();
    const second = openStream();
    script.answerStream(first);
    script.answerStream(second);
    const { qc } = mount(USER);
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    await advance(0);
    first.end();
    await advance(2_000);
    expect(useRealtimeStatus.getState().sse).toBe("connected");
    await advance(250);
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("tries again the moment the browser is back online, not at the end of its backoff", async () => {
    script.answerThrow();
    script.answerThrow();
    script.answerThrow();
    const stream = openStream();
    script.answerStream(stream);
    mount(USER);
    await advance(6_000); // attempts at 0 s, 2 s, 6 s; the next waits 8 s
    expect(script.requests).toHaveLength(3);
    expect(useRealtimeStatus.getState().sse).toBe("down");
    await act(async () => {
      window.dispatchEvent(new Event("online"));
      await advance(0);
    });
    expect(script.requests).toHaveLength(4);
    expect(useRealtimeStatus.getState().sse).toBe("connected");
  });

  it("leaves a connected stream alone when the browser says it is online", async () => {
    script.answerStream(openStream());
    mount(USER);
    await advance(0);
    await act(async () => {
      window.dispatchEvent(new Event("online"));
      await advance(0);
    });
    expect(script.requests).toHaveLength(1);
  });

  it("unmounting stops the stream and returns the status to idle", async () => {
    const stream = openStream();
    script.answerStream(stream);
    const { unmount } = mount(USER);
    await advance(0);
    unmount();
    await advance(0);
    expect(stream.aborted).toBe(true);
    expect(useRealtimeStatus.getState().sse).toBe("idle");
    await advance(120_000);
    expect(script.requests).toHaveLength(1);
  });

  it("signing out mid-session stops the stream", async () => {
    const stream = openStream();
    script.answerStream(stream);
    const { qc } = mount(USER);
    await advance(0);
    await act(async () => {
      qc.setQueryData(keys.auth.me, null);
      await advance(0);
    });
    expect(stream.aborted).toBe(true);
    expect(useRealtimeStatus.getState().sse).toBe("idle");
  });
});

// What a tab does after the server says it is reading too often.
//
// Every assertion here counts REAL requests: `fetch` is stubbed at the global
// and each call is recorded with its method and the moment it left, so "the
// reads are held" means nothing reached the network, not that some flag was
// set. The two wirings are driven the way the product drives them — the typed
// SDK client for a portal read, the cloud-chat transport for the chat page's
// own — because a gate wired into one of them and not the other is a page that
// keeps the limiter refusing.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useChatAttachments, useWorkspaceState } from "@/api/chats";
import { useChatTemplates } from "@/api/chatTemplates";
import { api } from "@/api/client";
import { getChat, stopTurn } from "@/api/cloudChat/transport";
import { ApiError } from "@/api/errors";
import { UploadClient } from "@/api/filesUpload";
import { useMyPreferences } from "@/api/preferences";
import { createQueryClient } from "@/api/queryClient";
import { readPauseMs, readPauseRemainingMs, readsPaused, resetReadGate } from "@/api/readGate";
import { READ_PAUSE_CAP_MS, READ_PAUSE_DEFAULT_MS, RETRY_BACKOFF_FLOOR_MS } from "@/lib/limits";
import { cloudChatFiles } from "@/pages/workspace/chat/data/chatFiles";
import { useSearchQuery } from "@/pages/workspace/files/useSearchQuery";

interface Call {
  method: string;
  url: string;
  at: number;
}

/** Answers, in the order the stub hands them out; the last one repeats. */
type Answer = () => Response;

const ok = (): Answer => () =>
  new Response(JSON.stringify({ id: "c1" }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

const throttled =
  (retryAfter: string | null): Answer =>
  () => {
    const headers = new Headers({ "content-type": "application/json" });
    if (retryAfter !== null) headers.set("retry-after", retryAfter);
    return new Response(JSON.stringify({ error: { code: "rate_limited", message: "slow down" } }), {
      status: 429,
      headers,
    });
  };

function stubFetch(script: Answer[]): {
  calls: Call[];
  reads: () => number;
  writes: () => number;
} {
  const calls: Call[] = [];
  let next = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const method = (
        init?.method ?? (input instanceof Request ? input.method : "GET")
      ).toUpperCase();
      calls.push({ method, url, at: Date.now() });
      const answer = script[Math.min(next, script.length - 1)] ?? ok();
      next += 1;
      return Promise.resolve(answer());
    }),
  );
  return {
    calls,
    reads: () => calls.filter((c) => c.method === "GET").length,
    writes: () => calls.filter((c) => c.method !== "GET").length,
  };
}

/** Take the first refusal so the gate is armed, without leaving a floating
 *  rejection behind. Returns the error the caller saw. */
async function refusedRead(): Promise<unknown> {
  return getChat("c1").then(
    () => null,
    (err: unknown) => err,
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  resetReadGate();
});

afterEach(() => {
  cleanup();
  resetReadGate();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("a read the server refuses", () => {
  it("holds every other read for the wait it named, then lets them go", async () => {
    const net = stubFetch([throttled("7"), ok()]);

    const refusal = await refusedRead();
    expect(refusal).toBeInstanceOf(ApiError);
    expect((refusal as ApiError).status).toBe(429);
    expect(net.reads()).toBe(1);

    const held = getChat("c1");
    // Just short of the wait the server asked for: still nothing on the wire.
    await vi.advanceTimersByTimeAsync(6_900);
    expect(net.reads()).toBe(1);

    await vi.advanceTimersByTimeAsync(200);
    await expect(held).resolves.toEqual({ id: "c1" });
    expect(net.reads()).toBe(2);
  });

  it("holds the whole herd, and none of them arrive before the wait is spent", async () => {
    // The point of a gate rather than a per-query ladder: a chat page holds a
    // dozen reads and they are refused together, so they must WAIT together.
    const net = stubFetch([throttled("7"), ok()]);
    await refusedRead();

    const held = [getChat("a"), getChat("b"), getChat("c")];
    await vi.advanceTimersByTimeAsync(6_900);
    expect(net.reads()).toBe(1);

    await vi.advanceTimersByTimeAsync(200);
    await Promise.all(held);
    expect(net.reads()).toBe(4);
    for (const call of net.calls.slice(1)) expect(call.at).toBeGreaterThanOrEqual(7_000);
  });

  it("holds a typed SDK read too, not only the chat transport's", async () => {
    const net = stubFetch([throttled("7"), ok()]);
    await refusedRead();

    const held = api.GET("/api/v1/auth/sessions");
    await vi.advanceTimersByTimeAsync(6_900);
    expect(net.reads()).toBe(1);

    await vi.advanceTimersByTimeAsync(200);
    await held;
    expect(net.reads()).toBe(2);
  });

  it("lets the person's own write out while the reads are held", async () => {
    // The limiter classes reads and writes apart, and a send that queued behind
    // a refused poll would read as a broken product. The write leaves at once.
    const net = stubFetch([throttled("7"), ok()]);
    await refusedRead();

    const held = getChat("c1");
    const sent = stopTurn("c1");
    await vi.advanceTimersByTimeAsync(0);
    await expect(sent).resolves.toBeUndefined();
    expect(net.writes()).toBe(1);
    expect(net.reads()).toBe(1);

    await vi.advanceTimersByTimeAsync(7_000);
    await held;
  });

  it("is not armed by a refused WRITE", async () => {
    // A throttled send has its own visible retry; it must not also park every
    // read on the page.
    const net = stubFetch([throttled("7"), ok()]);
    await stopTurn("c1").then(
      () => null,
      () => null,
    );
    expect(readsPaused()).toBe(false);

    await expect(getChat("c1")).resolves.toEqual({ id: "c1" });
    expect(net.reads()).toBe(1);
  });

  it("never holds the shell's own session probe", async () => {
    // Nothing renders until the shell knows who is reading, and that read has
    // its own ladder that already honours the header — so a refusal some
    // background poll collected must not park a blank shell behind it.
    const net = stubFetch([throttled("30"), ok()]);
    await refusedRead();
    expect(readsPaused()).toBe(true);

    await api.GET("/api/v1/auth/me");
    expect(net.reads()).toBe(2);
    // …and the pause it did not wait for is still standing for everything else.
    expect(readsPaused()).toBe(true);
  });

  it("holds for the default when the server named no wait at all", async () => {
    const net = stubFetch([throttled(null), ok()]);
    await refusedRead();

    const held = getChat("c1");
    await vi.advanceTimersByTimeAsync(READ_PAUSE_DEFAULT_MS - 100);
    expect(net.reads()).toBe(1);

    await vi.advanceTimersByTimeAsync(100);
    await held;
    expect(net.reads()).toBe(2);
  });

  it("caps a wait the server asked for beyond the ceiling", async () => {
    const net = stubFetch([throttled("600"), ok()]);
    await refusedRead();

    const held = getChat("c1");
    await vi.advanceTimersByTimeAsync(READ_PAUSE_CAP_MS - 100);
    expect(net.reads()).toBe(1);

    await vi.advanceTimersByTimeAsync(100);
    await held;
    expect(net.reads()).toBe(2);
  });

  it("extends the pause when a second refusal lands inside the first", async () => {
    const net = stubFetch([throttled("2"), throttled("10"), ok()]);
    await refusedRead();

    // Refused again the moment the first wait is spent, for ten more seconds.
    const refusedAgain = getChat("c1").then(
      () => null,
      () => null,
    );
    await vi.advanceTimersByTimeAsync(2_000);
    expect(net.reads()).toBe(2);
    await refusedAgain;

    const later = getChat("c1");
    await vi.advanceTimersByTimeAsync(9_900);
    expect(net.reads()).toBe(2);

    await vi.advanceTimersByTimeAsync(200);
    await later;
    expect(net.reads()).toBe(3);
  });

  it("aborts at the gate rather than holding a request nobody is waiting for", async () => {
    stubFetch([throttled("30"), ok()]);
    await refusedRead();

    const controller = new AbortController();
    const held = api.GET("/api/v1/auth/sessions", { signal: controller.signal });
    const settled = held.then(
      () => "resolved",
      (err: unknown) => (err as { name?: string }).name,
    );
    controller.abort();
    await vi.advanceTimersByTimeAsync(0);
    expect(await settled).toBe("AbortError");
    expect(readsPaused()).toBe(true);
  });
});

describe("clearing the pause", () => {
  it("is cleared by the read that waited it out", async () => {
    stubFetch([throttled("7"), ok()]);
    await refusedRead();
    expect(readsPaused()).toBe(true);

    const held = getChat("c1");
    await vi.advanceTimersByTimeAsync(7_000);
    await held;
    expect(readsPaused()).toBe(false);
  });

  it("is NOT cleared by a read that was already in flight when it was armed", async () => {
    // The 200 answers a request that left before the refusal, so it says
    // nothing about the window the refusal opened — clearing on it would send
    // the rest of the herd straight back into the limiter.
    let release!: (value: Response) => void;
    const firstAnswer = new Promise<Response>((resolve) => {
      release = resolve;
    });
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : (input as Request).url;
        calls.push(url);
        if (calls.length === 1) return firstAnswer;
        return Promise.resolve(throttled("30")());
      }),
    );

    const early = getChat("early");
    await vi.advanceTimersByTimeAsync(0);
    await refusedRead();
    expect(readsPaused()).toBe(true);

    release(
      new Response(JSON.stringify({ id: "early" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await early;
    expect(readsPaused()).toBe(true);
    expect(readPauseRemainingMs()).toBeGreaterThan(0);
  });
});

describe("the wait a refusal is worth", () => {
  it.each<[string | null, number, string]>([
    ["7", 7_000, "the wait the server named"],
    [null, READ_PAUSE_DEFAULT_MS, "no header at all"],
    ["600", READ_PAUSE_CAP_MS, "a header past the ceiling"],
    ["0", RETRY_BACKOFF_FLOOR_MS, "a literal zero"],
    ["-5", RETRY_BACKOFF_FLOOR_MS, "a negative delta"],
    ["", RETRY_BACKOFF_FLOOR_MS, "an empty header"],
    ["soon", READ_PAUSE_DEFAULT_MS, "a header that is not a wait"],
  ])("reads %j as %ims — %s", (header, expected) => {
    expect(readPauseMs(header)).toBe(expected);
  });

  it("reads an HTTP-date spelling, and a date already past as the floor", () => {
    const now = Date.UTC(2026, 8, 21, 12, 0, 0);
    expect(readPauseMs(new Date(now + 9_000).toUTCString(), now)).toBe(9_000);
    expect(readPauseMs(new Date(now - 60_000).toUTCString(), now)).toBe(RETRY_BACKOFF_FLOOR_MS);
  });
});

// --- the callers -------------------------------------------------------------
//
// The gate is wired at two transports, so a module that reaches for `fetch`
// itself is a read that never meets it — which is how the storm was built in
// the first place, one reasonable-looking hook at a time. Every entry below is
// a read the portal really makes, started through its own module rather than
// through the transport, and every one of them has to WAIT for a refusal it did
// not collect itself.

const CHAT = "11111111-1111-1111-1111-111111111111";
const DRIVE = "22222222-2222-2222-2222-222222222222";
const PARENT = "33333333-3333-3333-3333-333333333333";
const SESSION = "44444444-4444-4444-4444-444444444444";

/** The read that arms the pause for the table: a route none of the callers
 *  below touches, so a caller may be driven once BEFORE the refusal without
 *  spending it. */
const ARMING_PATH = "/api/v1/auth/sessions";

/** A body every caller below can read without falling over — each of their
 *  shapes' fields, filled in just far enough to keep the walk going — so the
 *  table is about WHEN the request leaves and never about what came back. */
const anyShape = (): Answer => () =>
  new Response(
    JSON.stringify({
      id: "c1",
      items: [],
      value: [],
      next_cursor: null,
      files_node_id: "root-1",
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );

/** Every read answered, except the arming one, which is always refused. */
function stubTableFetch(): { reads: () => number } {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const method = (
        init?.method ?? (input instanceof Request ? input.method : "GET")
      ).toUpperCase();
      calls.push({ method, url, at: Date.now() });
      const path = new URL(url, "http://localhost").pathname;
      return Promise.resolve(path === ARMING_PATH ? throttled("7")() : anyShape()());
    }),
  );
  return { reads: () => calls.filter((c) => c.method === "GET").length };
}

/** Mount a hook on a real client, and hand back the teardown. */
function mount(hook: () => unknown): () => void {
  const client = createQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { unmount } = renderHook(hook, { wrapper });
  return () => {
    unmount();
    client.clear();
  };
}

interface Caller {
  /** What the reader is doing when this read goes out. */
  name: string;
  /** Driven once before the refusal, where a module has to be past a first hop
   *  for the read under test to be the next one it makes. */
  warmup?: () => Promise<void>;
  /** Start the read; answer with the teardown for anything it mounted. */
  start: () => () => void;
}

const CALLERS: readonly Caller[] = [
  { name: "the chat-template list", start: () => mount(() => useChatTemplates()) },
  { name: "a chat's saved workspace", start: () => mount(() => useWorkspaceState(CHAT)) },
  { name: "a chat's attachments", start: () => mount(() => useChatAttachments(CHAT)) },
  { name: "the reader's own preferences", start: () => mount(() => useMyPreferences()) },
  {
    name: "a search across the drive",
    start: () =>
      mount(() =>
        useSearchQuery({
          driveId: DRIVE,
          folderId: undefined,
          scope: "drive",
          text: "quarterly",
          debounceMs: 0,
        }),
      ),
  },
  ((): Caller => {
    // The port's first hop is the chat itself, which the transport already
    // holds — so it is walked once first, and what is under test here is the
    // folder read it makes NEXT, which is its own raw call.
    const port = cloudChatFiles();
    const walk = (path: string): Promise<unknown> =>
      port.locate?.(CHAT, path).catch(() => null) ?? Promise.resolve(null);
    return {
      name: "the chat's own view of its files",
      warmup: async () => void (await walk("shot.png")),
      start: () => {
        void walk("other.png");
        return () => undefined;
      },
    };
  })(),
];

describe("the reads a page makes, module by module", () => {
  it.each(CALLERS)("holds $name behind a refusal another read collected", async (caller) => {
    const net = stubTableFetch();
    if (caller.warmup) await caller.warmup();
    await api.GET(ARMING_PATH);
    expect(readPauseRemainingMs()).toBeGreaterThan(6_000);
    const armed = net.reads();

    const stop = caller.start();
    try {
      // Long enough for a mount, a debounce and a chained lookup to have gone
      // out — and short of the wait the server asked for.
      await vi.advanceTimersByTimeAsync(6_900);
      expect(net.reads()).toBe(armed);

      await vi.advanceTimersByTimeAsync(200);
      expect(net.reads()).toBeGreaterThan(armed);
    } finally {
      stop();
    }
  });
});

describe("an upload while the reads are held", () => {
  it("keeps sending the person's file, reads and all", async () => {
    // The one caller deliberately left OUT of the gate. Every read the upload
    // client makes serves the file on screen: the run opens by asking what the
    // server already holds, so holding that read holds the bytes behind it.
    // Parking someone's upload because a folder poll was refused is the same
    // failure the gate refuses to inflict on a send — and a refusal this client
    // meets itself is already waited out on the server's terms.
    //
    // Driven through the client's DEFAULT transport, because the default is the
    // decision: an injected `fetchImpl` would prove nothing about it.
    const seen: { method: string; path: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const href =
          typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
        const path = new URL(href, "http://localhost").pathname;
        const method = (
          init?.method ?? (input instanceof Request ? input.method : "GET")
        ).toUpperCase();
        seen.push({ method, path });
        const json = (body: unknown): Response =>
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        // The refusal that arms the gate: a chat read, nothing to do with the
        // upload, which is the whole point.
        if (path.startsWith("/api/v1/chats/")) return throttled("30")();
        if (method === "POST" && path === "/api/v1/files/uploads") {
          return json({ uploadId: SESSION, partSize: 8, partsTotal: 1, expiresAt: "" });
        }
        if (method === "PUT") return json({ partNo: 1, size: 8, duplicate: false });
        if (method === "GET" && path === `/api/v1/files/uploads/${SESSION}`) {
          return json({
            uploadId: SESSION,
            state: "open",
            offset: seen.some((c) => c.method === "PUT") ? 8 : 0,
            length: 8,
            complete: seen.some((c) => c.method === "PUT"),
            partsDone: seen.some((c) => c.method === "PUT") ? 1 : 0,
            partsTotal: 1,
            acceptedParts: seen.some((c) => c.method === "PUT") ? [1] : [],
          });
        }
        if (method === "POST" && path.endsWith("/complete")) {
          return json({ item: { id: "node-1", etag: "etag-1" } });
        }
        return json({});
      }),
    );

    await refusedRead();
    expect(readPauseRemainingMs()).toBeGreaterThan(20_000);

    const cells = new Map<string, string>();
    const client = new UploadClient({
      digest: () => Promise.resolve("d1"),
      storage: {
        getItem: (key) => cells.get(key) ?? null,
        setItem: (key, value) => void cells.set(key, value),
        removeItem: (key) => void cells.delete(key),
      },
    });
    const file = new File([new Uint8Array(8).fill(65)], "one.bin", {
      type: "application/octet-stream",
    });
    const done = client
      .start(file, PARENT)
      .then((handle) => handle.done())
      .catch((err: unknown) => err);

    // Well inside a thirty-second pause: the session opened, the server was
    // asked what it holds, and the bytes went out — none of it waited.
    await vi.advanceTimersByTimeAsync(1_000);
    expect(seen.some((c) => c.method === "POST" && c.path === "/api/v1/files/uploads")).toBe(true);
    expect(
      seen.some((c) => c.method === "GET" && c.path === `/api/v1/files/uploads/${SESSION}`),
    ).toBe(true);
    expect(seen.some((c) => c.method === "PUT")).toBe(true);
    expect(await done).toMatchObject({ state: "done" });
    // …and the pause the upload ignored is still standing for the page's polls.
    expect(readsPaused()).toBe(true);
  });
});

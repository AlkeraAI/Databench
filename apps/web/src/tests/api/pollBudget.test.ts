// What an idle chat page is allowed to ask the server for.
//
// The page's reads are driven by one event stream and one machine poll, so the
// budget is a property of the query layer rather than of any one screen: a read
// the server has already answered with "gone" is never asked again, a refusal
// is re-asked on the server's own terms, a hidden tab asks for nothing, and two
// callers wanting the same list share one request.

import {
  focusManager,
  QueryClient,
  QueryObserver,
  type QueryKey,
} from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chatListChanged, listAllChats } from "@/api/cloudChat/transport";
import { ApiError } from "@/api/errors";
import { createInvalidationScheduler, EVENT_KEYS } from "@/api/events/eventMap";
import { isGone } from "@/api/gone";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { queryRetryDelay, retryAfterMs } from "@/api/retry";
import {
  MACHINE_POLL_MS,
  RETRY_BACKOFF_CAP_MS,
  RETRY_BACKOFF_FLOOR_MS,
  RETRY_JITTER_RATIO,
} from "@/lib/limits";

/** A mutation that declares no `meta.invalidates` — the page's default, and the
 *  one that re-asks for everything in the cache. */
async function anyMutation(client: QueryClient): Promise<void> {
  await client
    .getMutationCache()
    .build(client, { mutationFn: async () => "ok" })
    .execute(undefined);
  await settle(client);
}

/** Mount a read the way a page mounts it, so an invalidation really re-asks
 *  for it. An UNMOUNTED read is only marked stale, which proves nothing. */
function mounted(
  client: QueryClient,
  queryKey: QueryKey,
  queryFn: () => Promise<unknown>,
  options: { retry?: number | false } = {},
) {
  const observer = new QueryObserver(client, {
    queryKey,
    queryFn,
    retry: options.retry ?? false,
  });
  return observer.subscribe(() => undefined);
}

/** Let the refetches an invalidation queued actually run. */
async function settle(client: QueryClient): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await client.getQueryCache().getAll().length;
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function gone(status: number): ApiError {
  return new ApiError(status, { code: "not_found", message: "no such thing" });
}

function throttled(retryAfter: string | null): ApiError {
  const headers = new Headers();
  if (retryAfter !== null) headers.set("retry-after", retryAfter);
  return new ApiError(429, { code: "rate_limited", message: "slow down" }, "throttled", headers);
}

describe("a resource the server says is gone", () => {
  it("is asked for once, however many times the cache is invalidated", async () => {
    const client = createQueryClient();
    const calls = vi.fn(() => Promise.reject(gone(404)));
    const close = mounted(client, keys.chats.one("dead"), calls);
    await settle(client);
    expect(calls).toHaveBeenCalledTimes(1);

    // The chat page's own event stream invalidates the chat family many times a
    // minute while the box writes files, and every mutation on the page
    // invalidates everything. Neither may re-ask for a chat the server has
    // already refused — they name it by PREFIX and say nothing about it.
    const scheduler = createInvalidationScheduler(client);
    for (let i = 0; i < 5; i += 1) {
      scheduler.push({
        type: "file_node.changed",
        entity: "file_node",
        entity_id: `n${i}`,
        version: i,
        org_id: "o1",
        drive_id: "d1",
        parent_id: "folder",
      });
      scheduler.flush();
      await settle(client);
    }
    await anyMutation(client);
    expect(calls).toHaveBeenCalledTimes(1);
    scheduler.dispose();
    close();
    client.clear();
  });

  it("covers a 410 as well as a 404, and nothing else", () => {
    expect(isGone(gone(404))).toBe(true);
    expect(isGone(gone(410))).toBe(true);
    expect(isGone(gone(403))).toBe(false);
    expect(isGone(gone(500))).toBe(false);
    expect(isGone(throttled(null))).toBe(false);
    expect(isGone(new Error("offline"))).toBe(false);
  });

  it("is still re-read after a refusal that is not an answer", async () => {
    const client = createQueryClient();
    const calls = vi.fn(() => Promise.reject(gone(500)));
    const close = mounted(client, keys.chats.one("wobbly"), calls);
    await settle(client);
    const first = calls.mock.calls.length;
    await anyMutation(client);
    expect(calls.mock.calls.length).toBeGreaterThan(first);
    close();
    client.clear();
  });

  it("is left out of the invalidate-everything a mutation fires", async () => {
    const client = createQueryClient();
    const calls = vi.fn(() => Promise.reject(gone(410)));
    const close = mounted(client, keys.chats.one("dead"), calls);
    await settle(client);
    await anyMutation(client);
    await anyMutation(client);
    expect(calls).toHaveBeenCalledTimes(1);
    close();
    client.clear();
  });
});

describe("a throttled read", () => {
  it("waits the seconds the server named, plus jitter and never less", () => {
    const delay = queryRetryDelay(0, throttled("2"), () => 0);
    expect(delay).toBeGreaterThanOrEqual(2_000);
    expect(queryRetryDelay(0, throttled("2"), () => 1)).toBeLessThanOrEqual(
      2_000 * (1 + RETRY_JITTER_RATIO),
    );
  });

  it("reads an HTTP-date Retry-After as well as a delta in seconds", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-20T00:00:00Z"));
    const delay = queryRetryDelay(0, throttled("Sun, 20 Sep 2026 00:00:30 GMT"), () => 0);
    expect(delay).toBeGreaterThanOrEqual(30_000);
    vi.useRealTimers();
  });

  // Every one of these is a legal thing for a proxy or a CDN to put on a 429,
  // and every one of them reads as "no wait at all". Taken at their word they
  // re-send the refused burst instantly, three times over — which is worse than
  // having honoured no header, because react-query's own ladder starts at a
  // second. The floor is what makes honouring the header safe.
  it.each([
    ["an empty header", ""],
    ["a header of only space", " "],
    ["a literal zero", "0"],
    ["a negative delta", "-5"],
    ["a date this clock has already passed", "Sun, 20 Sep 2026 00:00:30 GMT"],
  ])("never re-asks sooner than the floor for %s", (_name, header) => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-20T00:02:00Z"));
    for (const attempt of [0, 1, 2]) {
      expect(queryRetryDelay(attempt, throttled(header), () => 0)).toBeGreaterThanOrEqual(
        RETRY_BACKOFF_FLOOR_MS,
      );
    }
    vi.useRealTimers();
  });

  it("reports the wait the server named honestly, floor or no floor", () => {
    // The floor belongs to the delay, not to the reading: a caller asking what
    // the server said must not be told it said a second when it said nothing.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-20T00:02:00Z"));
    expect(retryAfterMs(throttled("0"))).toBe(0);
    expect(retryAfterMs(throttled("-5"))).toBe(0);
    expect(retryAfterMs(throttled("Sun, 20 Sep 2026 00:00:30 GMT"))).toBe(0);
    expect(retryAfterMs(throttled("nonsense"))).toBeNull();
    expect(retryAfterMs(throttled(null))).toBeNull();
    expect(retryAfterMs(new Error("offline"))).toBeNull();
    vi.useRealTimers();
  });

  it("climbs and is jittered when the server named nothing", () => {
    const first = queryRetryDelay(0, throttled(null), () => 0);
    const second = queryRetryDelay(1, throttled(null), () => 0);
    expect(first).toBe(RETRY_BACKOFF_FLOOR_MS);
    expect(second).toBe(RETRY_BACKOFF_FLOOR_MS * 2);
    expect(queryRetryDelay(20, throttled(null), () => 1)).toBeLessThanOrEqual(
      RETRY_BACKOFF_CAP_MS * (1 + RETRY_JITTER_RATIO),
    );
    // Two tabs refused in the same second must not come back in the same one.
    expect(queryRetryDelay(0, throttled(null), () => 1)).toBeGreaterThan(
      queryRetryDelay(0, throttled(null), () => 0),
    );
  });
});

describe("the client every surface is built from", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("waits the server's Retry-After before a refused read is asked again", async () => {
    const client = createQueryClient();
    const calls = vi.fn(() => Promise.reject(throttled("5")));
    const close = mounted(client, keys.files.searchAll, calls, { retry: 1 });

    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toHaveBeenCalledTimes(1);
    // react-query's own ladder would have re-asked a whole second in.
    await vi.advanceTimersByTimeAsync(4_999);
    expect(calls).toHaveBeenCalledTimes(1);
    // 5 s, plus at most the jitter on top of it.
    await vi.advanceTimersByTimeAsync(5_000 * RETRY_JITTER_RATIO + 1);
    expect(calls).toHaveBeenCalledTimes(2);

    close();
    client.clear();
  });

  it("re-asks a plainly failed read on its own ladder", async () => {
    const client = createQueryClient();
    const calls = vi.fn(() => Promise.reject(gone(500)));
    const close = mounted(client, keys.files.searchAll, calls, { retry: 1 });

    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(RETRY_BACKOFF_FLOOR_MS - 1);
    expect(calls).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(RETRY_BACKOFF_FLOOR_MS * RETRY_JITTER_RATIO + 1);
    expect(calls).toHaveBeenCalledTimes(2);

    close();
    client.clear();
  });
});

describe("the chat family's invalidation fan-out", () => {
  it("does not drag this reader's tab layout along with the chat list", async () => {
    const client = createQueryClient();
    const layout = vi.fn(() => Promise.resolve({ tabs: [] }));
    await client.fetchQuery({ queryKey: keys.chatWorkspace.one("c1"), queryFn: layout });
    await client.invalidateQueries({ queryKey: keys.chats.all, refetchType: "all" });
    expect(layout).toHaveBeenCalledTimes(1);
    client.clear();
  });
});

describe("the event stream's scheduler", () => {
  let client: QueryClient;

  beforeEach(() => {
    vi.useFakeTimers();
    client = createQueryClient();
  });
  afterEach(() => {
    client.clear();
    vi.useRealTimers();
  });

  it("re-reads only the node and its folder when a machine landed bytes", () => {
    const machineRate = EVENT_KEYS["file_node.changed"]({
      type: "file_node.changed",
      entity: "file_node",
      entity_id: "n1",
      version: 1,
      org_id: "o1",
      drive_id: "d1",
      parent_id: "folder",
      reason: "live_saved",
    });
    expect(machineRate).toHaveLength(2);
    // Nothing a machine writing a file can change: who may read it, what this
    // reader starred or trashed, or which chats they may list.
    const flat = JSON.stringify(machineRate);
    for (const absent of ["permissions", "starred", "trash", "shared", "search", "chats"]) {
      expect(flat).not.toContain(absent);
    }
  });

  it("re-reads the whole family when a person changed a node", () => {
    const byHand = EVENT_KEYS["file_node.changed"]({
      type: "file_node.changed",
      entity: "file_node",
      entity_id: "n1",
      version: 1,
      org_id: "o1",
      drive_id: "d1",
      parent_id: "folder",
    });
    const flat = JSON.stringify(byHand);
    for (const present of ["permissions", "starred", "trash", "shared", "search", "chats"]) {
      expect(flat).toContain(present);
    }
  });

  it("costs a deleted folder one re-read per window, not one per frame", async () => {
    const children = vi.fn(() => Promise.reject(gone(404)));
    const close = mounted(client, keys.files.childrenOf("d1", "folder"), children);
    await vi.advanceTimersByTimeAsync(0);
    expect(children).toHaveBeenCalledTimes(1);

    // Ten frames about files inside it. Each one NAMES this folder's listing —
    // the server is talking about its contents, so asking again is right — but
    // they coalesce, so the burst costs one read and not ten.
    const scheduler = createInvalidationScheduler(client);
    for (let i = 0; i < 10; i += 1) {
      scheduler.push({
        type: "file_node.changed",
        entity: "file_node",
        entity_id: "n1",
        version: i,
        org_id: "o1",
        drive_id: "d1",
        parent_id: "folder",
      });
    }
    scheduler.flush();
    await vi.advanceTimersByTimeAsync(0);
    expect(children).toHaveBeenCalledTimes(2);
    scheduler.dispose();
    close();
  });

  it("leaves a gone listing alone when the frames are about another folder", async () => {
    const children = vi.fn(() => Promise.reject(gone(404)));
    const close = mounted(client, keys.files.childrenOf("d1", "folder"), children);
    await vi.advanceTimersByTimeAsync(0);

    const scheduler = createInvalidationScheduler(client);
    for (let i = 0; i < 10; i += 1) {
      scheduler.push({
        type: "file_node.changed",
        entity: "file_node",
        entity_id: `n${i}`,
        version: i,
        org_id: "o1",
        drive_id: "d1",
        parent_id: "elsewhere",
      });
      scheduler.flush();
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(children).toHaveBeenCalledTimes(1);
    scheduler.dispose();
    close();
  });

  // This repo answers a grantable denial with an opaque 404 (the files policy
  // refuses as-not-found whenever READ is not allowed), so "you may read this
  // after all" cannot arrive as anything else. A frame naming the node is the
  // server speaking about that node, and it has to be asked again.
  it("re-reads a node a frame names, although the last answer was a 404", async () => {
    let allowed = false;
    const node = vi.fn(() => (allowed ? Promise.resolve({ id: "n1" }) : Promise.reject(gone(404))));
    const close = mounted(client, keys.files.item("n1"), node);
    await vi.advanceTimersByTimeAsync(0);
    expect(node).toHaveBeenCalledTimes(1);

    allowed = true; // a share lands
    const scheduler = createInvalidationScheduler(client);
    scheduler.push({
      type: "file_node.changed",
      entity: "file_node",
      entity_id: "n1",
      version: 1,
      org_id: "o1",
      drive_id: "d1",
      parent_id: "folder",
    });
    scheduler.flush();
    await vi.advanceTimersByTimeAsync(0);

    expect(node).toHaveBeenCalledTimes(2);
    expect(client.getQueryData(keys.files.item("n1"))).toEqual({ id: "n1" });
    scheduler.dispose();
    close();
  });

  it("re-reads a chat a frame names, although the last answer was a 404", async () => {
    let allowed = false;
    const row = vi.fn(() => (allowed ? Promise.resolve({ id: "c1" }) : Promise.reject(gone(404))));
    const close = mounted(client, keys.chats.one("c1"), row);
    await vi.advanceTimersByTimeAsync(0);

    allowed = true;
    const scheduler = createInvalidationScheduler(client);
    scheduler.push({
      type: "chat.updated",
      entity: "chat",
      entity_id: "c1",
      version: 1,
      org_id: "o1",
    });
    scheduler.flush();
    await vi.advanceTimersByTimeAsync(0);

    expect(row).toHaveBeenCalledTimes(2);
    expect(client.getQueryData(keys.chats.one("c1"))).toEqual({ id: "c1" });
    scheduler.dispose();
    close();
  });

  it("re-reads everything, gone or not, when the stream was lost", async () => {
    const node = vi.fn(() => Promise.reject(gone(404)));
    const close = mounted(client, keys.files.item("n1"), node);
    await vi.advanceTimersByTimeAsync(0);
    expect(node).toHaveBeenCalledTimes(1);

    const scheduler = createInvalidationScheduler(client);
    scheduler.reset();
    scheduler.flush();
    await vi.advanceTimersByTimeAsync(0);

    // A gap is the one pass where this client does not know what happened.
    expect(node).toHaveBeenCalledTimes(2);
    scheduler.dispose();
    close();
  });

  it("still leaves a deleted chat alone under the storm that named it only by prefix", async () => {
    const row = vi.fn(() => Promise.reject(gone(404)));
    const close = mounted(client, keys.chats.one("dead"), row);
    await vi.advanceTimersByTimeAsync(0);
    expect(row).toHaveBeenCalledTimes(1);

    // A person-caused node frame names ["chats"], which PREFIXES the dead row.
    const scheduler = createInvalidationScheduler(client);
    for (let i = 0; i < 20; i += 1) {
      scheduler.push({
        type: "file_node.changed",
        entity: "file_node",
        entity_id: `n${i}`,
        version: i,
        org_id: "o1",
        drive_id: "d1",
        parent_id: "folder",
      });
      scheduler.flush();
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(row).toHaveBeenCalledTimes(1);
    scheduler.dispose();
    close();
  });

  it("asks for a named key once, not once per pass", async () => {
    const list = vi.fn(() => Promise.resolve([]));
    const close = mounted(client, keys.chats.all, list);
    await vi.advanceTimersByTimeAsync(0);
    expect(list).toHaveBeenCalledTimes(1);

    const scheduler = createInvalidationScheduler(client);
    scheduler.push({
      type: "chat.updated",
      entity: "chat",
      entity_id: "c1",
      version: 1,
      org_id: "o1",
    });
    scheduler.flush();
    await vi.advanceTimersByTimeAsync(0);
    // The exact pass and the prefix pass must not both fetch it.
    expect(list).toHaveBeenCalledTimes(2);
    scheduler.dispose();
    close();
  });
});

describe("two readers of the one chat list", () => {
  it("share the walk that is already in flight", async () => {
    const pages = vi.fn(async () => ({ items: [], next_cursor: null }));
    const both = await Promise.all([listAllChats(pages), listAllChats(pages)]);
    expect(pages).toHaveBeenCalledTimes(1);
    expect(both[0]).toEqual(both[1]);

    // A later read is a real read, not the first one's answer again.
    await listAllChats(pages);
    expect(pages).toHaveBeenCalledTimes(2);
  });

  it("does not hand one reader's walk to a caller reading it another way", async () => {
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const mine = vi.fn(async () => {
      await held;
      return { items: [{ id: "mine" }], next_cursor: null };
    });
    const yours = vi.fn(async () => ({ items: [{ id: "yours" }], next_cursor: null }));

    const a = listAllChats(mine as never);
    const b = listAllChats(yours as never);
    release?.();
    const [first, second] = await Promise.all([a, b]);

    expect(yours).toHaveBeenCalledTimes(1);
    expect(first.items[0]).toMatchObject({ id: "mine" });
    expect(second.items[0]).toMatchObject({ id: "yours" });
  });

  it("does not hand a walk that began before a write to a read that follows it", async () => {
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const stale = { items: [{ id: "deleted-chat" }], next_cursor: null };
    const fresh = { items: [], next_cursor: null };
    // Each walk is answered with what the server held when the walk STARTED.
    const answers = [stale, fresh];
    const pages = vi.fn(async () => {
      const mine = answers.shift() ?? fresh;
      await held;
      return mine;
    });

    const before = listAllChats(pages as never);
    // The delete lands while that walk is still out.
    chatListChanged();
    const after = listAllChats(pages as never);
    release?.();

    expect((await before).items).toEqual(stale.items);
    // The rail must not be shown the chat it just deleted.
    expect((await after).items).toHaveLength(0);
    expect(pages).toHaveBeenCalledTimes(2);
  });

  it("does not remember a walk that failed", async () => {
    const pages = vi.fn(async () => {
      throw gone(500);
    });
    await Promise.all([
      listAllChats(pages).catch(() => undefined),
      listAllChats(pages).catch(() => undefined),
    ]);
    await listAllChats(pages).catch(() => undefined);
    expect(pages).toHaveBeenCalledTimes(2);
  });
});

describe("an idle minute on an open chat", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    focusManager.setFocused(undefined);
    vi.useRealTimers();
  });

  /** The chat page's reads, mounted the way the page mounts them: two that poll
   *  on the machine floor, and three that only ever move when an event says so. */
  function openTheChatPage(client: QueryClient, count: () => void) {
    const polled = [keys.chats.one("c1"), keys.machines.current];
    const evented = [keys.chats.all, keys.chatWorkspace.one("c1"), keys.files.childrenOf("d", "f")];
    const observers = [
      ...polled.map(
        (queryKey) =>
          new QueryObserver(client, {
            queryKey,
            queryFn: async () => {
              count();
              return {};
            },
            refetchInterval: MACHINE_POLL_MS,
          }),
      ),
      ...evented.map(
        (queryKey) =>
          new QueryObserver(client, {
            queryKey,
            queryFn: async () => {
              count();
              return {};
            },
          }),
      ),
    ];
    const stops = observers.map((observer) => observer.subscribe(() => undefined));
    return () => stops.forEach((stop) => stop());
  }

  it("asks for eight things a minute and no more", async () => {
    const client = createQueryClient();
    let requests = 0;
    const close = openTheChatPage(client, () => {
      requests += 1;
    });
    await vi.advanceTimersByTimeAsync(0);
    const onMount = requests;
    expect(onMount).toBe(5);

    await vi.advanceTimersByTimeAsync(60_000);
    // Only the two machine-floor reads re-ask, four times each in a minute.
    expect(requests - onMount).toBe((60_000 / MACHINE_POLL_MS) * 2);
    close();
    client.clear();
  });

  it("asks for nothing at all while the tab is hidden", async () => {
    const client = createQueryClient();
    let requests = 0;
    const close = openTheChatPage(client, () => {
      requests += 1;
    });
    await vi.advanceTimersByTimeAsync(0);
    const onMount = requests;

    focusManager.setFocused(false);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(requests).toBe(onMount);
    close();
    client.clear();
  });
});

// The throttle every surface shares for reads a machine's saves ask for.
//
// Pinned against a real QueryClient with an observed query per key, so what
// is asserted is the reads that actually ran: a leading read, one trailing
// read per window however many asks, nothing for a version the cache already
// holds, and nothing fetched for a live document's own write-back while this
// tab co-edits it (only a stale mark, so the next reader is fresh).

import { QueryClient, QueryObserver, type InfiniteData } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import {
  createRefreshThrottle,
  holdLiveNode,
  isMachineRate,
  listingIsCurrent,
  itemIsCurrent,
  refreshForSave,
  setMachineRefresh,
  type ThrottleTimers,
} from "@/api/events/machineRefresh";
import type { ChildrenPage, Item } from "@/api/files";
import { keys } from "@/api/keys";

const DRIVE = "drv";
const FOLDER = "nd_folder";
const FILE = "nd_file";

/** A clock the test moves, with timers that fire when it passes them. */
function fakeClock(): ThrottleTimers & { advance: (ms: number) => void } {
  let now = 1_000_000;
  let pending: { at: number; fn: () => void; id: number }[] = [];
  let next = 0;
  return {
    now: () => now,
    setTimeout: (fn, ms) => {
      next += 1;
      pending.push({ at: now + ms, fn, id: next });
      return next;
    },
    clearTimeout: (id) => {
      pending = pending.filter((one) => one.id !== id);
    },
    advance(ms) {
      now += ms;
      const due = pending.filter((one) => one.at <= now);
      pending = pending.filter((one) => one.at > now);
      due.forEach((one) => one.fn());
    },
  };
}

let client: QueryClient;
let reads: Record<string, number>;
let unsubscribe: (() => void)[];

/** Keep `queryKey` observed (an active query) and count each run of it. */
function observe(queryKey: readonly unknown[], data: () => unknown): void {
  const name = JSON.stringify(queryKey);
  reads[name] = 0;
  const observer = new QueryObserver(client, {
    queryKey,
    queryFn: () => {
      reads[name] = (reads[name] ?? 0) + 1;
      return data();
    },
    staleTime: Infinity,
  });
  unsubscribe.push(observer.subscribe(() => undefined));
}

const readsOf = (queryKey: readonly unknown[]) => reads[JSON.stringify(queryKey)] ?? 0;
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const item = (etag: string): Item => ({ id: FILE, etag }) as unknown as Item;
const listing = (etag: string | null): InfiniteData<ChildrenPage> => ({
  pages: [{ value: etag === null ? [] : [item(etag)], nextMarker: null } as unknown as ChildrenPage],
  pageParams: [undefined],
});

function saved(version: number, reason = "live_saved"): RealtimeEventFrame {
  return {
    type: "file_node.changed",
    entity: "file_node",
    entity_id: FILE,
    version,
    org_id: "org",
    drive_id: DRIVE,
    parent_id: FOLDER,
    reason,
  } as RealtimeEventFrame;
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  reads = {};
  unsubscribe = [];
});

afterEach(() => {
  unsubscribe.forEach((stop) => stop());
  client.clear();
});

describe("createRefreshThrottle", () => {
  it("reads at once after a quiet spell, then once per window however many asks", async () => {
    const clock = fakeClock();
    const throttle = createRefreshThrottle(client, { windowMs: 10_000, timers: clock });
    const key = keys.files.item(FILE);
    observe(key, () => item("1"));
    await settle();
    const opened = readsOf(key);

    throttle.request(key);
    await settle();
    expect(readsOf(key)).toBe(opened + 1);

    for (let i = 0; i < 9; i += 1) {
      clock.advance(1_000);
      throttle.request(key);
    }
    await settle();
    expect(readsOf(key)).toBe(opened + 1);

    clock.advance(1_000);
    await settle();
    expect(readsOf(key)).toBe(opened + 2);

    // Quiet for a whole window: the next ask is read at once again.
    clock.advance(20_000);
    throttle.request(key);
    await settle();
    expect(readsOf(key)).toBe(opened + 3);
  });

  it("keeps each key to its own window", async () => {
    const clock = fakeClock();
    const throttle = createRefreshThrottle(client, { windowMs: 10_000, timers: clock });
    const one = keys.files.item("a");
    const two = keys.files.item("b");
    observe(one, () => item("1"));
    observe(two, () => item("1"));
    await settle();
    throttle.request(one);
    throttle.request(two);
    await settle();
    expect([readsOf(one), readsOf(two)]).toEqual([2, 2]);
  });

  it("drops trailing reads on dispose", async () => {
    const clock = fakeClock();
    const throttle = createRefreshThrottle(client, { windowMs: 10_000, timers: clock });
    const key = keys.files.item(FILE);
    observe(key, () => item("1"));
    await settle();
    throttle.request(key);
    throttle.request(key);
    throttle.dispose();
    clock.advance(10_000);
    await settle();
    expect(readsOf(key)).toBe(2);
  });
});

describe("what the cache already holds", () => {
  it.each([
    ["the same version", "5", 5, true],
    ["a newer version", "6", 5, true],
    ["an older version", "4", 5, false],
    ["a tag that is not a version", "W/abc", 5, false],
    ["a frame with no version", "5", 0, false],
  ])("an item at %s", (_, etag, version, current) => {
    client.setQueryData(keys.files.item(FILE), item(etag));
    expect(itemIsCurrent(client, FILE, version)).toBe(current);
  });

  it("an item that was never read is not current", () => {
    expect(itemIsCurrent(client, FILE, 1)).toBe(false);
  });

  it("a listing is current only when every cached listing shows the row at the version", () => {
    expect(listingIsCurrent(client, DRIVE, FOLDER, FILE, 5)).toBe(false);
    client.setQueryData(keys.files.children(DRIVE, FOLDER, {}, 50), listing("5"));
    expect(listingIsCurrent(client, DRIVE, FOLDER, FILE, 5)).toBe(true);
    client.setQueryData(keys.files.children(DRIVE, FOLDER, { orderBy: "name" } as never, 50), listing("4"));
    expect(listingIsCurrent(client, DRIVE, FOLDER, FILE, 5)).toBe(false);
  });

  it("a listing without the row is not current: the node may be new to it", () => {
    client.setQueryData(keys.files.children(DRIVE, FOLDER, {}, 50), listing(null));
    expect(listingIsCurrent(client, DRIVE, FOLDER, FILE, 5)).toBe(false);
  });
});

describe("refreshForSave", () => {
  beforeEach(() => {
    setMachineRefresh(client, createRefreshThrottle(client, { windowMs: 10_000, timers: fakeClock() }));
  });

  it("reads neither the item nor the listing when both already hold the version", async () => {
    observe(keys.files.item(FILE), () => item("5"));
    observe(keys.files.children(DRIVE, FOLDER, {}, 50), () => listing("5"));
    await settle();
    refreshForSave(client, saved(5), { listing: { driveId: DRIVE, parentId: FOLDER } });
    await settle();
    expect(readsOf(keys.files.item(FILE))).toBe(1);
    expect(readsOf(keys.files.children(DRIVE, FOLDER, {}, 50))).toBe(1);
  });

  it("reads both when the frame is newer", async () => {
    observe(keys.files.item(FILE), () => item("5"));
    observe(keys.files.children(DRIVE, FOLDER, {}, 50), () => listing("5"));
    await settle();
    refreshForSave(client, saved(6), { listing: { driveId: DRIVE, parentId: FOLDER } });
    await settle();
    expect(readsOf(keys.files.item(FILE))).toBe(2);
    expect(readsOf(keys.files.children(DRIVE, FOLDER, {}, 50))).toBe(2);
  });

  it("fetches nothing for a live document's write-back of a file this tab co-edits, but marks it stale", async () => {
    observe(keys.files.item(FILE), () => item("5"));
    await settle();
    const release = holdLiveNode(FILE);
    refreshForSave(client, saved(6, "live_doc_saved"));
    await settle();
    expect(readsOf(keys.files.item(FILE))).toBe(1);
    expect(client.getQueryState(keys.files.item(FILE))?.isInvalidated).toBe(true);
    release();
  });

  it("reads a live document's write-back once nobody here co-edits the file", async () => {
    observe(keys.files.item(FILE), () => item("5"));
    await settle();
    const release = holdLiveNode(FILE);
    release();
    release();
    refreshForSave(client, saved(6, "live_doc_saved"));
    await settle();
    expect(readsOf(keys.files.item(FILE))).toBe(2);
  });

  it("still reads a machine's save of a file this tab co-edits", async () => {
    observe(keys.files.item(FILE), () => item("5"));
    await settle();
    const release = holdLiveNode(FILE);
    refreshForSave(client, saved(6, "live_saved"));
    await settle();
    expect(readsOf(keys.files.item(FILE))).toBe(2);
    release();
  });
});

describe("isMachineRate", () => {
  it.each([
    ["live_saved", true],
    ["live_batch", true],
    ["conflict", true],
    ["inbound_superseded", true],
    ["live_doc_saved", true],
    [undefined, false],
    ["something_else", false],
  ])("a node frame with reason %s", (reason, expected) => {
    const frame = { ...saved(1), reason } as RealtimeEventFrame;
    expect(isMachineRate(frame)).toBe(expected);
  });

  it("every lease frame, and no other kind of frame", () => {
    expect(isMachineRate({ type: "file_lease.changed" } as RealtimeEventFrame)).toBe(true);
    expect(isMachineRate({ type: "chat.updated", reason: "live_saved" } as unknown as RealtimeEventFrame)).toBe(
      false,
    );
  });
});


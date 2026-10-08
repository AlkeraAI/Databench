// The registry behind a live draft and a live file: one handle per key shared
// by every holder, a linger after the last one lets go, a fresh handle in place
// of one that gave up, and one call that lets every handle go.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { liveHandleRegistry, type LiveHandleRegistry } from "@/api/realtime/crdt/liveHandles";

const LINGER_MS = 30_000;

class Handle {
  closed = 0;
  spent = false;
  constructor(readonly key: string) {}
  /** Something typed the server has not taken: the handle may not be let go. */
  unacknowledged = false;
  close(): void {
    this.closed += 1;
  }
  holdsUnacknowledged(): boolean {
    return this.unacknowledged;
  }
}

let registry: LiveHandleRegistry<Handle>;
let opened: Handle[];

function hold(key = "k1"): { handle: Handle; release: () => void } {
  return registry.acquire(key, () => {
    const handle = new Handle(key);
    opened.push(handle);
    return handle;
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  opened = [];
  registry = liveHandleRegistry<Handle>({ lingerMs: LINGER_MS, isSpent: (h) => h.spent });
});

afterEach(() => {
  registry.closeAll();
  vi.useRealTimers();
});

describe("liveHandleRegistry", () => {
  it("shares one handle between every holder of a key, and keeps keys apart", () => {
    const a = hold("k1");
    const b = hold("k1");
    const c = hold("k2");
    expect(a.handle).toBe(b.handle);
    expect(c.handle).not.toBe(a.handle);
    expect(opened.map((h) => h.key)).toEqual(["k1", "k2"]);
  });

  it("keeps the handle while any holder is left, however long", () => {
    const a = hold();
    hold();
    a.release();
    vi.advanceTimersByTime(LINGER_MS * 10);
    expect(a.handle.closed).toBe(0);
    expect(hold().handle).toBe(a.handle);
  });

  it("closes the handle once the linger after the last release runs out, and not a moment before", () => {
    const a = hold();
    a.release();
    vi.advanceTimersByTime(LINGER_MS - 1);
    expect(a.handle.closed).toBe(0);
    vi.advanceTimersByTime(1);
    expect(a.handle.closed).toBe(1);
    // Gone: the next hold opens a new one.
    const next = hold();
    expect(next.handle).not.toBe(a.handle);
    expect(opened).toHaveLength(2);
  });

  it("a hold inside the linger keeps the same handle, and the old linger never closes it", () => {
    const a = hold();
    a.release();
    vi.advanceTimersByTime(LINGER_MS - 1);
    const again = hold();
    expect(again.handle).toBe(a.handle);
    vi.advanceTimersByTime(LINGER_MS * 2);
    expect(a.handle.closed).toBe(0);
    // Its own release starts a fresh, full linger.
    again.release();
    vi.advanceTimersByTime(LINGER_MS - 1);
    expect(a.handle.closed).toBe(0);
    vi.advanceTimersByTime(1);
    expect(a.handle.closed).toBe(1);
  });

  it("counts a release once however many times it is called", () => {
    const a = hold();
    const b = hold();
    a.release();
    a.release();
    // `b` still holds: a doubled release must not have let the handle go.
    vi.advanceTimersByTime(LINGER_MS);
    expect(b.handle.closed).toBe(0);
  });

  it("replaces a spent handle nobody holds, at once and closing it once", () => {
    const a = hold();
    a.release();
    a.handle.spent = true;
    const next = hold();
    expect(next.handle).not.toBe(a.handle);
    expect(a.handle.closed).toBe(1);
    // The abandoned linger does not close it a second time, nor the new one.
    vi.advanceTimersByTime(LINGER_MS);
    expect(a.handle.closed).toBe(1);
    expect(next.handle.closed).toBe(0);
  });

  it("keeps a spent handle somebody still holds", () => {
    const a = hold();
    a.handle.spent = true;
    expect(hold().handle).toBe(a.handle);
    expect(a.handle.closed).toBe(0);
  });

  it("never closes an unheld handle that holds something unacknowledged, and closes it one linger after it holds nothing", () => {
    const a = hold();
    a.handle.unacknowledged = true;
    a.release();
    vi.advanceTimersByTime(LINGER_MS * 5);
    expect(a.handle.closed).toBe(0);
    // A holder inside that long wait is handed the same handle.
    const again = hold();
    expect(again.handle).toBe(a.handle);
    again.release();
    a.handle.unacknowledged = false;
    vi.advanceTimersByTime(LINGER_MS);
    expect(a.handle.closed).toBe(1);
    expect(hold().handle).not.toBe(a.handle);
  });

  it("hands a spent handle that still owes its reader something to the next holder instead of replacing it", () => {
    const a = hold();
    a.release();
    a.handle.spent = true;
    a.handle.unacknowledged = true;
    expect(hold().handle).toBe(a.handle);
    expect(a.handle.closed).toBe(0);
    expect(opened).toHaveLength(1);
  });

  it("closeAll lets go even of a handle that holds something unacknowledged (the session ended)", () => {
    const a = hold();
    a.handle.unacknowledged = true;
    a.release();
    registry.closeAll();
    expect(a.handle.closed).toBe(1);
  });

  it("closeAll lets every handle go, held or lingering, and forgets them", () => {
    const held = hold("k1");
    const lingering = hold("k2");
    lingering.release();
    registry.closeAll();
    expect(held.handle.closed).toBe(1);
    expect(lingering.handle.closed).toBe(1);
    // The lingering handle's timer was dropped: it is never closed twice.
    vi.advanceTimersByTime(LINGER_MS);
    expect(lingering.handle.closed).toBe(1);
    // A late release of a forgotten handle neither closes it again nor touches the new one.
    const fresh = hold("k1");
    held.release();
    vi.advanceTimersByTime(LINGER_MS);
    expect(held.handle.closed).toBe(1);
    expect(fresh.handle).not.toBe(held.handle);
    expect(fresh.handle.closed).toBe(0);
  });
});

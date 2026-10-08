// An outdated tab reloads into the current build — once, and never while someone types.

import { describe, expect, it, vi } from "vitest";

import {
  type ClientGenerationDeps,
  RELOAD_IDLE_MS,
  onMinClientGeneration,
} from "@/api/realtime/clientGeneration";

class FakeDoc {
  visibilityState: DocumentVisibilityState = "visible";
  private listeners = new Map<string, Set<() => void>>();
  addEventListener(name: string, fn: () => void): void {
    let set = this.listeners.get(name);
    if (!set) this.listeners.set(name, (set = new Set()));
    set.add(fn);
  }
  removeEventListener(name: string, fn: () => void): void {
    this.listeners.get(name)?.delete(fn);
  }
  emit(name: string): void {
    for (const fn of [...(this.listeners.get(name) ?? [])]) fn();
  }
  listenerCount(): number {
    return [...this.listeners.values()].reduce((n, s) => n + s.size, 0);
  }
}

function harness(opts: { generation?: number; storageWorks?: boolean } = {}) {
  vi.useFakeTimers();
  const store = new Map<string, string>();
  const doc = new FakeDoc();
  const reload = vi.fn();
  const deps: ClientGenerationDeps = {
    generation: opts.generation ?? 1,
    storage: {
      get: (k) => store.get(k) ?? null,
      set: (k, v) => {
        if (opts.storageWorks === false) return false;
        store.set(k, v);
        return true;
      },
    },
    reload,
    doc: doc as unknown as ClientGenerationDeps["doc"],
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  };
  return { deps, doc, reload, store };
}

describe("onMinClientGeneration", () => {
  it.each([
    ["the server's minimum is this build's", 1],
    ["the server's minimum is older", 0],
    ["the value is not an integer", 1.5],
    ["the value is not a number at all", Number.NaN],
  ])("does nothing when %s", (_name, min) => {
    const { deps, reload, doc } = harness();
    expect(onMinClientGeneration(min, deps)).toBe("current");
    vi.advanceTimersByTime(RELOAD_IDLE_MS * 10);
    expect(reload).not.toHaveBeenCalled();
    expect(doc.listenerCount()).toBe(0);
  });

  it("reloads an older build once the person has stopped typing for a moment", () => {
    const { deps, reload, doc } = harness();
    expect(onMinClientGeneration(2, deps)).toBe("reloading");
    vi.advanceTimersByTime(RELOAD_IDLE_MS - 1);
    doc.emit("input"); // typing pushes the reload back: a debounced draft must get out first
    vi.advanceTimersByTime(RELOAD_IDLE_MS - 1);
    doc.emit("compositionupdate");
    vi.advanceTimersByTime(RELOAD_IDLE_MS - 1);
    expect(reload).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(doc.listenerCount()).toBe(0);
  });

  it("reloads at once when the tab is hidden, and never twice", () => {
    const { deps, reload, doc } = harness();
    onMinClientGeneration(2, deps);
    doc.visibilityState = "hidden";
    doc.emit("visibilitychange");
    expect(reload).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(RELOAD_IDLE_MS * 2);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("reloads immediately when the tab is already hidden", () => {
    const { deps, reload, doc } = harness();
    doc.visibilityState = "hidden";
    expect(onMinClientGeneration(2, deps)).toBe("reloading");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("does not reload in a loop when the reload landed on an equally old build", () => {
    const first = harness();
    onMinClientGeneration(2, first.deps);
    vi.advanceTimersByTime(RELOAD_IDLE_MS);
    expect(first.reload).toHaveBeenCalledTimes(1);
    // The same tab session after the reload: the marker survived, the build is still 1.
    const again = { ...first.deps, reload: vi.fn() };
    expect(onMinClientGeneration(2, again)).toBe("stale");
    vi.advanceTimersByTime(RELOAD_IDLE_MS * 2);
    expect(again.reload).not.toHaveBeenCalled();
    // A newer server generation is a new reason to reload, once.
    expect(onMinClientGeneration(3, again)).toBe("reloading");
  });

  it("stays put when the browser will not keep the marker", () => {
    const { deps, reload } = harness({ storageWorks: false });
    expect(onMinClientGeneration(2, deps)).toBe("stale");
    vi.advanceTimersByTime(RELOAD_IDLE_MS * 2);
    expect(reload).not.toHaveBeenCalled();
  });
});

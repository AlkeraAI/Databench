import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePresence } from "./usePresence";

// rAF shim driven by fake timers so the two-frame enter flip is steppable.
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) =>
    window.setTimeout(() => cb(performance.now()), 16),
  );
  vi.stubGlobal("cancelAnimationFrame", (id: number) => window.clearTimeout(id));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("usePresence", () => {
  it("a node that MOUNTS already-open is born closed and flips open on the next frames (the enter animates, no teleport)", () => {
    const { result } = renderHook(() => usePresence(true));
    // First commit paints the closed start-state — this is what makes the slide play.
    expect(result.current).toEqual({ mounted: true, state: "closed" });
    act(() => {
      vi.advanceTimersByTime(40);
    });
    expect(result.current).toEqual({ mounted: true, state: "open" });
  });

  it("closed → open after mount also runs through the closed start-state", () => {
    const { result, rerender } = renderHook(({ open }) => usePresence(open), {
      initialProps: { open: false },
    });
    expect(result.current).toEqual({ mounted: false, state: "closed" });
    rerender({ open: true });
    expect(result.current.state).toBe("closed");
    act(() => {
      vi.advanceTimersByTime(40);
    });
    expect(result.current).toEqual({ mounted: true, state: "open" });
  });

  it("close keeps the node mounted through the exit window, then unmounts", () => {
    const { result, rerender } = renderHook(({ open }) => usePresence(open), {
      initialProps: { open: true },
    });
    act(() => {
      vi.advanceTimersByTime(40);
    });
    rerender({ open: false });
    expect(result.current).toEqual({ mounted: true, state: "closed" });
    act(() => {
      vi.advanceTimersByTime(140);
    });
    expect(result.current.mounted).toBe(false);
  });
});

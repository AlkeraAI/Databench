import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SEARCH_DEBOUNCE_MS, useDebouncedValue } from "./useDebouncedValue";

// Every search field in the product waits out this one hook, so the two things a surface reads
// off it -- the settled value, and whether it has caught up -- are the whole contract.

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

const typing = (initial = "") =>
  renderHook(({ value }) => useDebouncedValue(value, SEARCH_DEBOUNCE_MS), {
    initialProps: { value: initial },
  });

describe("useDebouncedValue", () => {
  it("holds the older value, and says so, until the typing stops for the delay", () => {
    const { result, rerender } = typing();
    rerender({ value: "rev" });
    expect(result.current).toEqual(["", false]);

    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS));
    expect(result.current).toEqual(["rev", true]);
  });

  it("charges one settle for a burst, because each keystroke restarts the wait", () => {
    const { result, rerender } = typing();
    for (const value of ["r", "re", "rev"]) {
      rerender({ value });
      act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS - 1));
    }
    // Two full delays have passed in total and nothing has settled: only the pause counts.
    expect(result.current).toEqual(["", false]);

    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toEqual(["rev", true]);
  });

  it("resolves a cleared value in the same render, so emptying a field costs no wait", () => {
    // A frame late is long enough for anything latching onto the settled value -- a frozen row
    // order, a scroll target -- to latch the narrower set that is already gone.
    const { result, rerender } = typing();
    rerender({ value: "rev" });
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS));

    rerender({ value: "" });
    expect(result.current).toEqual(["", true]);
  });
});

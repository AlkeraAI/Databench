import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { browserOnline, onBrowserOnline, useBrowserOnline } from "@/lib/online";

function setOnline(online: boolean): void {
  vi.stubGlobal("navigator", { ...navigator, onLine: online });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("browserOnline", () => {
  it.each([
    { label: "online", nav: { onLine: true }, expected: true },
    { label: "offline", nav: { onLine: false }, expected: false },
    { label: "a browser with no such property", nav: {}, expected: true },
    { label: "no navigator at all", nav: undefined, expected: true },
  ])("reads $label as $expected", ({ nav, expected }) => {
    vi.stubGlobal("navigator", nav);
    expect(browserOnline()).toBe(expected);
  });
});

describe("onBrowserOnline", () => {
  it("calls each handler on its own event, and nothing after it is let go", () => {
    const seen: string[] = [];
    const stop = onBrowserOnline({ online: () => seen.push("online"), offline: () => seen.push("offline") });
    window.dispatchEvent(new Event("offline"));
    window.dispatchEvent(new Event("online"));
    stop();
    window.dispatchEvent(new Event("offline"));
    window.dispatchEvent(new Event("online"));
    expect(seen).toEqual(["offline", "online"]);
  });

  it("listens only for the handlers it was given", () => {
    const online = vi.fn();
    const stop = onBrowserOnline({ online });
    window.dispatchEvent(new Event("offline"));
    expect(online).not.toHaveBeenCalled();
    window.dispatchEvent(new Event("online"));
    expect(online).toHaveBeenCalledOnce();
    stop();
  });
});

describe("useBrowserOnline", () => {
  it("follows the network as it goes and comes back", () => {
    setOnline(true);
    const { result } = renderHook(() => useBrowserOnline());
    expect(result.current).toBe(true);
    act(() => {
      setOnline(false);
      window.dispatchEvent(new Event("offline"));
    });
    expect(result.current).toBe(false);
    act(() => {
      setOnline(true);
      window.dispatchEvent(new Event("online"));
    });
    expect(result.current).toBe(true);
  });
});

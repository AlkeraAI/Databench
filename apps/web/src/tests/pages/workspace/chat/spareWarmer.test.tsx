// The chat page's heartbeat for the chat warmed ahead of the first message:
// it beats when the page is on screen, every minute while it stays, not at all
// while the tab is hidden, and again the moment it comes back — and the empty
// composer's send asks to claim what was warmed. Nothing on screen ever
// changes for it: a refused beat is swallowed.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createChat } from "@/api/cloudChat/transport";
import { SPARE_HEARTBEAT_MS, useSpareWarmer } from "@/pages/workspace/chat/useSpareWarmer";

function warmCalls(): number {
  const fetchMock = vi.mocked(fetch);
  return fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/api/v1/chats/spare"))
    .length;
}

/** Let an in-flight beat settle. Beats are single-flight, so a second one
 *  raised before the first resolves is deliberately the same beat. */
async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

function setVisibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("the spare heartbeat", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ state: "warm" }), { status: 200 })),
    );
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("beats once on mount and once a minute while the page is visible", async () => {
    renderHook(() => useSpareWarmer(true));
    expect(warmCalls()).toBe(1);
    await settle();
    act(() => {
      vi.advanceTimersByTime(SPARE_HEARTBEAT_MS);
    });
    expect(warmCalls()).toBe(2);
    await settle();
    act(() => {
      vi.advanceTimersByTime(SPARE_HEARTBEAT_MS - 1);
    });
    expect(warmCalls()).toBe(2);
  });

  it("stops while the tab is hidden and beats again when it comes back", async () => {
    renderHook(() => useSpareWarmer(true));
    expect(warmCalls()).toBe(1);
    await settle();
    act(() => setVisibility("hidden"));
    act(() => {
      vi.advanceTimersByTime(3 * SPARE_HEARTBEAT_MS);
    });
    expect(warmCalls()).toBe(1);
    await settle();
    act(() => setVisibility("visible"));
    expect(warmCalls()).toBe(2);
  });

  it("beats nothing when disabled, and stops on unmount", async () => {
    const disabled = renderHook(() => useSpareWarmer(false));
    expect(warmCalls()).toBe(0);
    disabled.unmount();
    await settle();
    const enabled = renderHook(() => useSpareWarmer(true));
    expect(warmCalls()).toBe(1);
    enabled.unmount();
    act(() => {
      vi.advanceTimersByTime(2 * SPARE_HEARTBEAT_MS);
    });
    expect(warmCalls()).toBe(1);
  });

  it("swallows a refused beat: nothing throws, the next beat still goes", async () => {
    vi.mocked(fetch).mockImplementation(
      async () => new Response(JSON.stringify({ detail: "slow down" }), { status: 429 }),
    );
    renderHook(() => useSpareWarmer(true));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(SPARE_HEARTBEAT_MS);
    });
    expect(warmCalls()).toBe(2);
  });

  it("raises one beat, not two, when the warmer mounts twice in a tick", async () => {
    // What StrictMode does in development — mount, tear down, mount again —
    // and what a second chat page open in the same tab would do. Two beats in
    // the same tick say the same thing, and the server answers the second by
    // handing back the first one's spare; one call is enough.
    renderHook(() => useSpareWarmer(true));
    renderHook(() => useSpareWarmer(true));
    expect(warmCalls()).toBe(1);
    await settle();
    // And the guard lets go: the next beat still goes out.
    act(() => {
      vi.advanceTimersByTime(SPARE_HEARTBEAT_MS);
    });
    expect(warmCalls()).toBeGreaterThan(1);
  });
});

describe("the create body", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ id: "c1" }), { status: 201 })),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("carries claim_spare only when the caller asked to claim", async () => {
    await createChat(null, { claimSpare: true, effort: "high" });
    await createChat("Ops", {});
    const bodies = vi
      .mocked(fetch)
      .mock.calls.map(([, init]) => JSON.parse(String(init?.body)) as Record<string, unknown>);
    expect(bodies[0]).toEqual({ title: null, effort: "high", claim_spare: true });
    expect(bodies[1]).toEqual({ title: "Ops" });
  });
});

// The shared socket: one instance, started while anyone holds it, lingering half a minute
// after the last release so a reader flipping between entries pays one handshake.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetRealtimeStatus, useRealtimeStatus } from "@/api/events/status";
import {
  REALTIME_CLIENT_LINGER_MS,
  acquireRealtimeClient,
  getRealtimeClient,
  setRealtimeClientForTests,
} from "@/api/realtime/client";
import { WsClient } from "@/api/realtime/wsClient";

import { socketFactory, ticketMinter } from "./fakeWebSocket";

function fakeClient() {
  const client = new WsClient({
    mintTicket: ticketMinter().mint,
    socketUrl: (path) => `ws://api.test${path}`,
    factory: socketFactory().factory,
  });
  const start = vi.spyOn(client, "start");
  const stop = vi.spyOn(client, "stop");
  return { client, start, stop };
}

beforeEach(() => {
  vi.useFakeTimers();
  resetRealtimeStatus();
});

afterEach(() => {
  setRealtimeClientForTests(null);
  vi.useRealTimers();
});

describe("the shared realtime client", () => {
  it("getRealtimeClient hands back one instance, created on first use and not started", () => {
    const first = getRealtimeClient();
    expect(getRealtimeClient()).toBe(first);
    expect(first.status).toBe("idle");
  });

  it("the default instance mirrors its status into the store's socket row", () => {
    const client = getRealtimeClient();
    // No WebSocket factory is injected here; in an environment without one the client reports
    // down at once — either way the store hears it.
    client.start();
    expect(useRealtimeStatus.getState().ws).toBe(client.status);
    client.stop();
  });

  it("acquire starts the client once for any number of holders", () => {
    const { client, start } = fakeClient();
    setRealtimeClientForTests(client);
    const releaseA = acquireRealtimeClient();
    const releaseB = acquireRealtimeClient();
    expect(start).toHaveBeenCalledTimes(2); // start is idempotent; the client ran once
    expect(client.status).toBe("connecting");
    releaseA();
    releaseB();
  });

  it("the client stops only after the last release plus the linger, not before", () => {
    const { client, stop } = fakeClient();
    setRealtimeClientForTests(client);
    const releaseA = acquireRealtimeClient();
    const releaseB = acquireRealtimeClient();
    releaseA();
    vi.advanceTimersByTime(REALTIME_CLIENT_LINGER_MS * 2);
    expect(stop).not.toHaveBeenCalled();
    releaseB();
    vi.advanceTimersByTime(REALTIME_CLIENT_LINGER_MS - 1);
    expect(stop).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(stop).toHaveBeenCalledTimes(1);
    expect(client.status).toBe("idle");
  });

  it("a re-acquire inside the linger cancels the stop", () => {
    const { client, stop } = fakeClient();
    setRealtimeClientForTests(client);
    acquireRealtimeClient()();
    vi.advanceTimersByTime(REALTIME_CLIENT_LINGER_MS - 1);
    const release = acquireRealtimeClient();
    vi.advanceTimersByTime(REALTIME_CLIENT_LINGER_MS * 2);
    expect(stop).not.toHaveBeenCalled();
    expect(client.status).toBe("connecting");
    release();
  });

  it("a release is idempotent", () => {
    const { client, stop } = fakeClient();
    setRealtimeClientForTests(client);
    const releaseA = acquireRealtimeClient();
    const releaseB = acquireRealtimeClient();
    releaseA();
    releaseA();
    releaseA();
    vi.advanceTimersByTime(REALTIME_CLIENT_LINGER_MS * 2);
    expect(stop).not.toHaveBeenCalled();
    releaseB();
    vi.advanceTimersByTime(REALTIME_CLIENT_LINGER_MS);
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it("setRealtimeClientForTests stops the instance it replaces and forgets the holders", () => {
    const { client, stop } = fakeClient();
    setRealtimeClientForTests(client);
    acquireRealtimeClient();
    const next = fakeClient();
    setRealtimeClientForTests(next.client);
    expect(stop).toHaveBeenCalledTimes(1);
    expect(getRealtimeClient()).toBe(next.client);
    const release = acquireRealtimeClient();
    expect(next.start).toHaveBeenCalledTimes(1);
    release();
    vi.advanceTimersByTime(REALTIME_CLIENT_LINGER_MS);
    expect(next.stop).toHaveBeenCalledTimes(1);
  });
});

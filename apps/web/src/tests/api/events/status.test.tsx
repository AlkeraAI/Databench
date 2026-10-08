// The realtime status store and the masthead pill that reads it. The polling predicate is
// "not live" — true before the first connect, during a reconnect, once the client gave up,
// and when signed out — so the fallback covers every moment nothing is being delivered; the
// pill is narrower: it appears only once the client has been trying past its grace window.

import { useState, type ReactNode } from "react";
import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { RealtimeStatusIndicator } from "@/api/events/RealtimeStatusIndicator";
import {
  isRealtimeDown,
  isStreamLive,
  resetRealtimeStatus,
  useRealtimeDown,
  useRealtimeStatus,
  type RealtimeStatus,
} from "@/api/events/status";
import { TopbarSlotsContext } from "@/app/Topbar";

const STATUSES: RealtimeStatus[] = ["idle", "connecting", "connected", "reconnecting", "down"];

beforeEach(resetRealtimeStatus);
afterEach(cleanup);

describe("useRealtimeStatus", () => {
  it("starts idle on both transports — nothing is live until a client says so", () => {
    expect(useRealtimeStatus.getState().sse).toBe("idle");
    expect(useRealtimeStatus.getState().ws).toBe("idle");
  });

  it.each(STATUSES)("isStreamLive(%s) is true only for connected", (status) => {
    expect(isStreamLive(status)).toBe(status === "connected");
  });

  it.each(STATUSES)("with the stream %s, the polling fallback runs iff it is not connected", (status) => {
    useRealtimeStatus.getState().setSse(status);
    expect(isRealtimeDown()).toBe(status !== "connected");
    const { result } = renderHook(() => useRealtimeDown());
    expect(result.current).toBe(status !== "connected");
  });

  it("the socket's status does not drive the polling predicate", () => {
    useRealtimeStatus.getState().setSse("connected");
    useRealtimeStatus.getState().setWs("down");
    expect(isRealtimeDown()).toBe(false);
  });

  it("useRealtimeDown re-renders its caller when the answer flips, and only then", () => {
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useRealtimeDown();
    });
    expect(result.current).toBe(true);
    const after = renders;
    act(() => useRealtimeStatus.getState().setSse("connecting")); // still not live: same answer
    expect(renders).toBe(after);
    act(() => useRealtimeStatus.getState().setSse("connected"));
    expect(result.current).toBe(false);
    expect(renders).toBe(after + 1);
    act(() => useRealtimeStatus.getState().setSse("down"));
    expect(result.current).toBe(true);
  });

  it("resetRealtimeStatus restores the initial state", () => {
    useRealtimeStatus.getState().setSse("down");
    useRealtimeStatus.getState().setWs("connected");
    resetRealtimeStatus();
    expect(useRealtimeStatus.getState().sse).toBe("idle");
    expect(useRealtimeStatus.getState().ws).toBe("idle");
  });
});

/** A masthead with a real actions slot for the pill to portal into. */
function Shell({ children }: { children: ReactNode }) {
  const [actions, setActions] = useState<HTMLElement | null>(null);
  return (
    <TopbarSlotsContext.Provider
      value={{ subtitle: null, actions, framed: true, setTitleHidden: () => {}, setTopbarHidden: () => {} }}
    >
      <div data-testid="actions" ref={setActions} />
      {children}
    </TopbarSlotsContext.Provider>
  );
}

describe("RealtimeStatusIndicator", () => {
  it("lands the pill in the masthead's actions slot once the stream is down", () => {
    render(
      <Shell>
        <RealtimeStatusIndicator />
      </Shell>,
    );
    expect(screen.queryByText("Live updates paused")).toBeNull();
    act(() => useRealtimeStatus.getState().setSse("down"));
    const pill = screen.getByRole("status");
    expect(pill).toHaveTextContent("Live updates paused");
    expect(screen.getByTestId("actions")).toContainElement(pill);
  });

  it.each(["idle", "connecting", "connected", "reconnecting"] as RealtimeStatus[])(
    "renders nothing while the stream is %s",
    (status) => {
      useRealtimeStatus.getState().setSse(status);
      render(
        <Shell>
          <RealtimeStatusIndicator />
        </Shell>,
      );
      expect(screen.queryByText("Live updates paused")).toBeNull();
    },
  );

  // The socket is the transport the CHAT rides: tokens, asks, the transcript.
  // A pong timeout takes it down for up to 45 s while the event stream stays
  // perfectly healthy — so a reader watching an answer stop had nothing on
  // screen to tell them the page was no longer live.
  it("lands the pill when the socket is down and the event stream is fine", () => {
    useRealtimeStatus.getState().setSse("connected");
    render(
      <Shell>
        <RealtimeStatusIndicator />
      </Shell>,
    );
    expect(screen.queryByText("Live updates paused")).toBeNull();
    act(() => useRealtimeStatus.getState().setWs("down"));
    expect(screen.getByRole("status")).toHaveTextContent("Live updates paused");
  });

  it("clears the pill when the socket comes back", () => {
    useRealtimeStatus.setState({ sse: "connected", ws: "down" });
    render(
      <Shell>
        <RealtimeStatusIndicator />
      </Shell>,
    );
    expect(screen.getByText("Live updates paused")).toBeInTheDocument();
    act(() => useRealtimeStatus.getState().setWs("connected"));
    expect(screen.queryByText("Live updates paused")).toBeNull();
  });

  it("disappears again when the stream comes back", () => {
    useRealtimeStatus.getState().setSse("down");
    render(
      <Shell>
        <RealtimeStatusIndicator />
      </Shell>,
    );
    expect(screen.getByText("Live updates paused")).toBeInTheDocument();
    act(() => useRealtimeStatus.getState().setSse("connected"));
    expect(screen.queryByText("Live updates paused")).toBeNull();
  });
});

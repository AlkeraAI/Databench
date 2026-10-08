import { act, cleanup, render, screen } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const { ds, hostRequest, live } = vi.hoisted(() => {
  // The fold's live reading, which the reaper cannot infer on its own.
  const live = {
    activity: {} as Record<string, { awaiting: boolean; lastInteractionAt: string | null }>,
  };
  const ds = { getChatActivity: vi.fn(() => live.activity) };
  const hostRequest = vi.fn(async () => ({}));
  return { ds, hostRequest, live };
});

vi.mock("./data", () => ({
  chatHost: () => ({ engine: { request: hostRequest } }),
 chatData: () => ds }));

import {
  REPORT_INTERVAL_MS,
  useReportChatActivity,
  viewedChatIdFromPath,
} from "./useReportChatActivity";

describe("viewedChatIdFromPath", () => {
  // Every editor overlay ABOUT a chat counts as viewing it, or the reaper would
  // collect a chat the user is inspecting. The id is decoded out of the path.
  it.each([
    ["/chat/abc-123", "abc-123"],
    ["/chat/abc-123/", "abc-123"],
    ["/chat/a%2Fb", "a/b"],
    ["/editor/chat/c1", "c1"],
    ["/editor/graph/c1", "c1"],
    ["/editor/activity/c1", "c1"],
    ["/editor/compaction/c1/p9", "c1"],
    ["/editor/blob/c1/h2", "c1"],
    ["/editor/blobs/c1", "c1"],
    ["/editor/plan/c1/p3", "c1"],
    // Surfaces that view no specific chat.
    ["/sidecar", null],
    ["/chat", null], // the new-chat composer has no id yet
    ["/editor/settings", null],
    ["/", null],
  ])("reads %s as %s", (path, id) => {
    expect(viewedChatIdFromPath(path)).toBe(id);
  });
});

/** A surface that reports, and can be sent to another route or back to its own. */
function Probe(): ReactElement {
  useReportChatActivity();
  const navigate = useNavigate();
  return createElement(
    "div",
    null,
    createElement("button", { onClick: () => navigate("/chat/b") }, "leave"),
    createElement("button", { onClick: () => navigate("/chat/a") }, "stay"),
  );
}

function mountAt(path: string): ReturnType<typeof render> {
  return render(createElement(MemoryRouter, { initialEntries: [path] }, createElement(Probe)));
}

const press = (name: string): void => void act(() => screen.getByRole("button", { name }).click());

/** Every report the host has been sent, in order. */
const reports = (): unknown[] => hostRequest.mock.calls.map((call) => (call as unknown[])[1]);

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
  live.activity = {};
});

describe("useReportChatActivity", () => {
  it("tells the host which chat is being read as soon as the surface mounts", () => {
    live.activity = { a: { awaiting: true, lastInteractionAt: null } };
    mountAt("/chat/a");

    expect(hostRequest).toHaveBeenCalledTimes(1);
    expect(hostRequest).toHaveBeenCalledWith("chat.reportActivity", {
      viewedChatId: "a",
      activity: live.activity,
    });
  });

  it("reports every chat's working state, not only the one on screen", () => {
    live.activity = {
      a: { awaiting: false, lastInteractionAt: "2026-06-10T00:00:00Z" },
      b: { awaiting: true, lastInteractionAt: null },
    };
    mountAt("/chat/a");

    expect(reports()).toEqual([{ viewedChatId: "a", activity: live.activity }]);
  });

  it("reports the new chat when the reader moves to one", () => {
    mountAt("/chat/a");
    press("leave");

    expect(reports()).toEqual([
      { viewedChatId: "a", activity: {} },
      { viewedChatId: "b", activity: {} },
    ]);
  });

  it("reports no chat once the reader leaves for a surface about none", () => {
    mountAt("/editor/blob/c1/h2");
    expect(reports()[0]).toEqual({ viewedChatId: "c1", activity: {} });
  });

  it("stays quiet when a navigation lands on the chat already being read", () => {
    mountAt("/chat/a");
    press("stay");
    press("stay");

    expect(hostRequest).toHaveBeenCalledTimes(1);
  });

  it("keeps reporting on its own clock, so a background chat that finishes is seen", async () => {
    vi.useFakeTimers();
    live.activity = { b: { awaiting: true, lastInteractionAt: null } };
    mountAt("/chat/a");
    expect(hostRequest).toHaveBeenCalledTimes(1);

    // The background chat finishes while the reader sits still.
    live.activity = { b: { awaiting: false, lastInteractionAt: "2026-06-10T00:00:03Z" } };
    await act(async () => void vi.advanceTimersByTime(REPORT_INTERVAL_MS));
    expect(reports()[1]).toEqual({ viewedChatId: "a", activity: live.activity });

    await act(async () => void vi.advanceTimersByTime(REPORT_INTERVAL_MS));
    expect(hostRequest).toHaveBeenCalledTimes(3);
  });

  it("goes silent once the surface unmounts", async () => {
    vi.useFakeTimers();
    const view = mountAt("/chat/a");
    view.unmount();

    await act(async () => void vi.advanceTimersByTime(REPORT_INTERVAL_MS * 3));
    expect(hostRequest).toHaveBeenCalledTimes(1);
  });

  it("keeps its clock running after the host refuses a report", async () => {
    vi.useFakeTimers();
    // The refusal is logged for diagnosability; the suite's output stays readable.
    const logged = vi.spyOn(console, "debug").mockImplementation(() => {});
    hostRequest.mockRejectedValueOnce(new Error("fixture: the daemon was restarting"));
    mountAt("/chat/a");

    await act(async () => void vi.advanceTimersByTime(REPORT_INTERVAL_MS));
    expect(hostRequest).toHaveBeenCalledTimes(2);
    logged.mockRestore();
  });
});

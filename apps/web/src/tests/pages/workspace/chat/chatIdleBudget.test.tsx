// What an open chat costs the server while nobody touches it.
//
// An idle chat must not trip the API's rate limiter: an event stream that
// invalidates the chat family several times a second while the box writes
// files would fan out into a burst of reads.
//
// The mechanisms that bound it are pinned in `apps/web/src/tests/api/pollBudget.test.ts`.
// This pins the OUTCOME, and it has to be measured on the real page: a model
// built out of hand-rolled observers restates its own setup and would stay
// green through a new `refetchInterval` added to any chat-page query, which is
// exactly the regression worth catching.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MACHINE_POLL_MS } from "@/api/chats";
import { createQueryClient } from "@/api/queryClient";

// The chat's own shell is stubbed, the page around it is not. What is measured
// here is every read the ROUTE starts — the rail's list, the drive, the machine
// and the chat row — which is where every one of the five busiest requests in
// the original measurement came from.
vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: (props: { chatId?: string }) => (
    <div data-testid="chat-surface" data-chat-id={props.chatId ?? ""} />
  ),
}));

const { ChatPage, ChatRoute } = await import("@/pages/workspace/chat/ChatPage");

const CHAT = "6f1a1c2e-6d41-4e2a-9a77-2b1f0c9d4e55";

const CHAT_ROW = {
  id: CHAT,
  title: "Revenue by region",
  owner_user_id: "u1",
  machine_id: "m1",
  machine_status: "ready",
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  last_seq: 0,
  permission_mode: "default",
  files_node_trashed: false,
};

/** Every read the page can make, answered; every call counted by path. */
function countingFetch() {
  const seen: string[] = [];
  const spy = vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input);
    const at = new URL(url, "http://x").pathname;
    seen.push(at);
    const json = (body: unknown, code = 200): Response =>
      new Response(JSON.stringify(body), {
        status: code,
        headers: { "content-type": "application/json" },
      });
    if (/^\/api\/v1\/chats\/[^/]+$/.test(at)) return json(CHAT_ROW);
    if (at.startsWith("/api/v1/chats")) return json({ items: [CHAT_ROW], next_cursor: null });
    if (at === "/api/v1/files/drives") {
      return json({ id: "drv_1", orgId: "org_1", rootId: "nd_root", quotaBytes: 1 });
    }
    if (at.includes("/machines/current")) {
      return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
    }
    if (at.includes("/auth/me")) return json({ id: "u1", email: "dana@example.com" });
    if (at === "/api/v1/workspaces") return json({ items: [], next_cursor: null });
    return json({});
  });
  vi.stubGlobal("fetch", spy);
  // `/api/v1/events` is the SSE stream, which jsdom opens through EventSource
  // rather than fetch, so it is not a request this counter sees.
  //
  // `/api/v1/ws/tickets` is left out on purpose: there is no WebSocket server
  // behind jsdom, so the chat socket sits in its reconnect ladder and re-mints
  // a ticket on each attempt. That is the reconnect path behaving correctly and
  // says nothing about an idle page, where the socket connects once.
  const counted = (at: string) => at.startsWith("/api/v1/") && at !== "/api/v1/ws/tickets";
  return {
    seen,
    apiCalls: () => seen.filter(counted),
    /** How many times each path was asked for, over a slice of the log. */
    breakdown: (from: number): Record<string, number> => {
      const out: Record<string, number> = {};
      for (const at of seen.filter(counted).slice(from)) out[at] = (out[at] ?? 0) + 1;
      return out;
    },
  };
}

function renderChat() {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[`/chat/${CHAT}`]}>
        <Routes>
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:chatId" element={<ChatRoute />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("an open chat nobody is touching", () => {
  it("asks the server for a small, constant number of things a minute", async () => {
    const wire = countingFetch();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderChat();
    await screen.findByTestId("chat-surface");
    await waitFor(() => expect(wire.apiCalls().length).toBeGreaterThan(0));

    // Let the page finish opening, then start counting from a settled screen.
    await vi.advanceTimersByTimeAsync(1_000);
    const opened = wire.apiCalls().length;

    await vi.advanceTimersByTimeAsync(60_000);
    const inAMinute = wire.apiCalls().length - opened;

    // Nine, and here is every one of them: the two reads on the machine floor
    // — the box's state and the chat row, which carries the binding — four
    // times each, plus the one heartbeat that keeps a chat warmed ahead of the
    // reader's first message. Nothing else. The list, the drive, the folder
    // listings and this reader's tab layout are all event-driven now, and an
    // idle chat produces no events.
    const onTheFloor = 60_000 / MACHINE_POLL_MS;
    expect(wire.breakdown(opened)).toEqual({
      "/api/v1/machines/current": onTheFloor,
      [`/api/v1/chats/${CHAT}`]: onTheFloor,
      "/api/v1/chats/spare": 1,
    });
    expect(inAMinute).toBe(9);

    // And the SECOND minute costs the same as the first: nothing accumulates
    // with how long the tab has been open.
    const before = wire.apiCalls().length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(wire.apiCalls().length - before).toBe(inAMinute);
  });

  it("asks for nothing at all while the reader is looking elsewhere", async () => {
    const wire = countingFetch();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderChat();
    await screen.findByTestId("chat-surface");
    await vi.advanceTimersByTimeAsync(1_000);

    // Hiding the tab for real: react-query's reads stop on the focus manager,
    // and the spare heartbeat stops on the document's own visibility.
    const { focusManager } = await import("@tanstack/react-query");
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    focusManager.setFocused(false);

    const hidden = wire.apiCalls().length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(wire.apiCalls().length).toBe(hidden);
    focusManager.setFocused(undefined);
  });
});

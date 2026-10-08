// What an id with no chat behind it costs.
//
// The dead end is drawn from one read. The page and the header actions each
// asked the server for the same row, so a single visit fired the same refused
// GET several times over — and every one of those rejections reached
// `window.onerror` as an uncaught `ApiError: Not found`, which is a handled,
// expected answer being logged as a client error by whatever watches that
// event.

import { cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The portal's own client, which is the one the chat data source reads the
// row through as well — a separate client here would make the two reads two
// caches, and the de-duplication under test unobservable.
import { queryClient } from "@/api/queryClient";

import { CHAT_GONE_TITLE, ChatPage, ChatRoute } from "@/pages/workspace/chat/ChatPage";

const UUID = "6f1a1c2e-6d41-4e2a-9a77-2b1f0c9d4e55";

function scriptFetch(): { asked: string[] } {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      asked.push(at);
      const json = (body: unknown, code = 200): Response =>
        new Response(JSON.stringify(body), {
          status: code,
          headers: { "content-type": "application/json" },
        });
      if (/^\/api\/v1\/chats\/[^/]+/.test(at)) return json({ detail: "Not found" }, 404);
      if (at.startsWith("/api/v1/chats")) return json({ items: [], next_cursor: null });
      if (at === "/api/v1/files/drives") {
        return json({ id: "drv_1", orgId: "org_1", rootId: "nd_root", quotaBytes: 1 });
      }
      if (at.includes("/machines/current")) {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      if (at.includes("/auth/me")) return json({ id: "u1", email: "dana@example.com" });
      return json({});
    }),
  );
  return { asked };
}

let rejections: unknown[] = [];
const onRejection = (event: PromiseRejectionEvent): void => {
  rejections.push(event.reason);
};

beforeEach(() => {
  queryClient.clear();
  rejections = [];
  window.addEventListener("unhandledrejection", onRejection);
});

afterEach(() => {
  window.removeEventListener("unhandledrejection", onRejection);
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/** Anything the browser queued as an unhandled rejection lands a macrotask
 *  later, so the assertion waits for that turn rather than the next microtask. */
async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 50));
}

describe("a visit to an id with no chat behind it", () => {
  it("asks once, renders the dead end, and leaves nothing unhandled", async () => {
    const { asked } = scriptFetch();
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/chat/${UUID}`]}>
          <Routes>
            <Route path="/chat/new" element={<ChatPage />} />
            <Route path="/chat/:chatId" element={<ChatRoute />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText(CHAT_GONE_TITLE)).toBeTruthy();
    await settle();

    // One read of the row, however many things on the page stand on it.
    expect(asked.filter((path) => path === `/api/v1/chats/${UUID}`)).toHaveLength(1);
    // And the refusal is an answer the page renders, not an error the browser
    // reports on its way out.
    expect(rejections).toEqual([]);
  });
});

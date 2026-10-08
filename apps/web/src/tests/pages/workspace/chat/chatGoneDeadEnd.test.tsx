// Where an id with no chat behind it leaves the reader.
//
// The dead end itself is right — there is nothing at that id for this reader,
// and saying so is the honest answer. The way out is what is pinned here, so a
// reader who followed a stale link keeps their rail: a sentence that says what happened to THIS chat, a primary key
// that opens an empty composer, and the rail left where it was.
//
// And the id is not asked about again. The chat row is polled every fifteen
// seconds because a machine's state changes under an open chat; a chat the
// server has already refused has no machine and no state, so the poll is a
// question with a permanent answer, asked forever, on a page that has stopped
// showing the chat.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { MACHINE_POLL_MS } from "@/api/chats";

// Stands in for "the chat shell mounted": every chat-scoped read the surface
// would start begins with this component, so its absence is the mechanism.
vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: (props: { chatId?: string }) => (
    <div data-testid="chat-surface" data-chat-id={props.chatId ?? ""}>
      <textarea aria-label="Message" />
    </div>
  ),
}));

import {
  CHAT_GONE_BODY,
  CHAT_GONE_TITLE,
  ChatPage,
  ChatRoute,
  NEW_CHAT,
  NEW_CHAT_PATH,
} from "@/pages/workspace/chat/ChatPage";

const UUID = "6f1a1c2e-6d41-4e2a-9a77-2b1f0c9d4e55";

/** The wire, with the chat row answering 404 and every other read fine — the
 *  rail included, because the rail is what has to survive the dead end. */
function scriptFetch(chats: { id: string; title: string }[]) {
  const spy = vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input);
    const at = new URL(url, "http://x").pathname;
    const json = (body: unknown, code = 200): Response =>
      new Response(JSON.stringify(body), {
        status: code,
        headers: { "content-type": "application/json" },
      });
    if (/^\/api\/v1\/chats\/[^/]+$/.test(at)) return json({ detail: "Not found" }, 404);
    if (at.startsWith("/api/v1/chats")) {
      return json({
        items: chats.map((chat) => ({
          id: chat.id,
          title: chat.title,
          owner_user_id: "u1",
          machine_id: null,
          machine_status: "none",
          created_at: "2026-09-06T12:00:00Z",
          updated_at: "2026-09-06T12:00:00Z",
          last_seq: 0,
          permission_mode: "default",
          files_node_trashed: false,
        })),
        next_cursor: null,
      });
    }
    if (at === "/api/v1/files/drives") {
      return json({ id: "drv_1", orgId: "org_1", rootId: "nd_root", quotaBytes: 1 });
    }
    if (at.includes("/machines/current")) {
      return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
    }
    if (at.includes("/auth/me")) return json({ id: "u1", email: "dana@example.com" });
    return json({});
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

function renderAt(path: string) {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/new" element={<ChatPage />} />
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

describe("the dead end an unknown chat id lands on", () => {
  it("says what happened to this chat rather than to the address", async () => {
    scriptFetch([]);
    renderAt(`/chat/${UUID}`);

    expect(await screen.findByText(CHAT_GONE_TITLE)).toBeTruthy();
    expect(screen.getByText(CHAT_GONE_BODY)).toBeTruthy();
    // Still nothing of the chat itself: the shell that would read a transcript,
    // a permission mode and a files dock for an id the server has refused.
    expect(screen.queryByTestId("chat-surface")).toBeNull();
    expect(screen.queryByLabelText("Message")).toBeNull();
    // And never the id, which is what keeps a deleted chat and a chat in
    // another org the same answer.
    expect(document.body.textContent ?? "").not.toContain(UUID);
  });

  it("offers a key that opens an empty composer, not a link out of chat", async () => {
    scriptFetch([]);
    renderAt(`/chat/${UUID}`);

    const key = await screen.findByRole("link", { name: NEW_CHAT });
    expect(key.getAttribute("href")).toBe(NEW_CHAT_PATH);
  });

  it("leaves the rail of the chats this reader still has", async () => {
    scriptFetch([{ id: "c1", title: "Revenue by region" }]);
    renderAt(`/chat/${UUID}`);

    await screen.findByText(CHAT_GONE_TITLE);
    // The rail is the way back to a chat that DOES exist; a dead end that takes
    // it away costs the reader every other chat as well as the missing one.
    expect(await screen.findByText("Revenue by region")).toBeTruthy();
  });

  it("stops asking for a chat the server has already refused", async () => {
    const spy = scriptFetch([]);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderAt(`/chat/${UUID}`);

    await screen.findByText(CHAT_GONE_TITLE);
    const asks = () =>
      spy.mock.calls.filter(([input]) => {
        const url = input instanceof Request ? input.url : String(input);
        return new URL(url, "http://x").pathname === `/api/v1/chats/${UUID}`;
      }).length;
    expect(asks()).toBe(1);

    // Four poll windows: a refusal that is re-asked would have answered four
    // more times by here, and a reader who leaves the tab open overnight would
    // have asked it thousands of times.
    await vi.advanceTimersByTimeAsync(MACHINE_POLL_MS * 4);
    await waitFor(() => expect(asks()).toBe(1));
  });
});

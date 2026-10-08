// A page stacked over a chat that is not there says the chat is gone.
//
// Each stacked page — the results list, one result, a plan, a compaction
// summary — reads only its own slice of the chat. Over an id the server has no
// chat for, each came back as its own empty or failed state: "No results yet"
// (a chat that exists and has nothing in it), "this result is not stored in the
// cloud yet — save it first" beside a live download key, "This chat could not
// be read.", "Plan not found." The chat page itself already answered the same
// id with one dead end. Every stacked page now answers it the same way — the
// same words for a deleted chat and one in another org, since both are the one
// opaque 404 — and with a way out.
//
// Driven through the real route tree, runtime and surfaces, with `fetch`
// scripted at the wire.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import appSource from "@/App.tsx?raw";
import { createQueryClient } from "@/api/queryClient";
import { CHAT_GONE_BODY, CHAT_GONE_TITLE, NEW_CHAT_PATH } from "@/pages/workspace/chat/ChatPage";

import { ChatRoutes } from "./chatRouteTree";

const UUID = "0f0e0d0c-0b0a-4000-8000-000000000011";

/** The chat row answers `chat`; every other read answers as an empty chat. */
function scriptFetch(chat: { status: number; body: unknown }): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      const json = (body: unknown, code = 200): Response =>
        new Response(JSON.stringify(body), { status: code, headers: { "content-type": "application/json" } });
      if (at.includes("/auth/me")) return json({ id: "u1", email: "dana@example.com" });
      if (at.includes("/machines/current")) return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      if (/^\/api\/v1\/chats\/[^/]+$/.test(at)) return json(chat.body, chat.status);
      // Every slice of a chat the server has no record of is refused too, as
      // it is on the wire; a page that read only its slice drew that refusal.
      if (at.startsWith(`/api/v1/chats/${UUID}/`)) {
        return chat.status === 200 ? json({ items: [], next_cursor: null }) : json({ detail: "Not found" }, chat.status);
      }
      if (at.startsWith("/api/v1/chats")) return json({ items: [], next_cursor: null });
      return json({});
    }),
  );
}

const CHAT_ROW = {
  id: UUID,
  title: "Quarterly review",
  owner_user_id: "u1",
  machine_id: "m1",
  machine_status: "ready",
  machine_refusal_reason: null,
  permission_mode: "default",
  last_seq: 0,
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
};

function renderAt(path: string) {
  return render(
    <StrictMode>
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <MemoryRouter initialEntries={[path]}>
          <ChatRoutes />
        </MemoryRouter>
      </QueryClientProvider>
    </StrictMode>,
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/** Each stacked page, with the words it drew for a chat that is not there. */
const STACKED = [
  { page: "results", path: `/chat/${UUID}/results`, own: /No results yet/ },
  { page: "one result", path: `/chat/${UUID}/result/nohandle`, own: /not stored in the cloud|save it first/i },
  { page: "compaction summary", path: `/chat/${UUID}/compaction/nopart`, own: /could not be read/ },
  { page: "plan", path: `/chat/${UUID}/plan/nopart`, own: /Plan not found/ },
];

describe.each(STACKED)("the $page page over a chat that is not there", ({ path, own }) => {
  it.each([
    ["deleted", "Chat not found"],
    ["in another organization", "No such chat in this organization"],
  ])("reads as gone when the chat is %s, with a way back to the chats", async (_why, detail) => {
    scriptFetch({ status: 404, body: { detail } });
    renderAt(path);

    expect(await screen.findByText(CHAT_GONE_TITLE)).toBeInTheDocument();
    expect(screen.getByText(CHAT_GONE_BODY)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New chat" })).toHaveAttribute("href", NEW_CHAT_PATH);
    // Nothing of the page's own state, and nothing of the server's body.
    expect(screen.queryByText(own)).toBeNull();
    expect(screen.queryByText(detail)).toBeNull();
    // No key that acts on the chat.
    expect(screen.queryByRole("button", { name: /download/i })).toBeNull();
  });

  it("says the chat is closed to this reader when the server says so", async () => {
    scriptFetch({ status: 403, body: { detail: "forbidden" } });
    renderAt(path);
    expect(await screen.findByText("You don't have access to this chat")).toBeInTheDocument();
    expect(screen.queryByText(CHAT_GONE_TITLE)).toBeNull();
    expect(screen.getByRole("link", { name: "Back to chats" })).toHaveAttribute("href", NEW_CHAT_PATH);
  });

  it("an address that cannot name a chat is a page this app does not have", async () => {
    scriptFetch({ status: 200, body: CHAT_ROW });
    renderAt(path.replace(UUID, "not-a-chat"));
    expect(await screen.findByText("This page doesn't exist")).toBeInTheDocument();
    expect(screen.queryByText(CHAT_GONE_TITLE)).toBeNull();
  });
});

describe("the results page over a chat that is there", () => {
  it("draws its own state, not the dead end", async () => {
    scriptFetch({ status: 200, body: CHAT_ROW });
    renderAt(`/chat/${UUID}/results`);
    await waitFor(() => expect(screen.getByText("No results yet")).toBeInTheDocument());
    expect(screen.queryByText(CHAT_GONE_TITLE)).toBeNull();
  });
});

describe("the app's own route table", () => {
  /** The `path` of every route nested in the stacked-page guard's block. */
  function guardedPaths(source: string): string[] {
    const open = source.indexOf("<Route element={<ChatStackedRoute />}>");
    expect(open).toBeGreaterThan(-1);
    const close = source.indexOf("</Route>", open);
    return [...source.slice(open, close).matchAll(/path="([^"]+)"/g)].map((m) => m[1]!);
  }

  it("puts every page under a chat's id behind the guard", () => {
    const underAChat = [...appSource.matchAll(/path="(\/chat\/:chatId\/[^"]+)"/g)].map((m) => m[1]!);
    expect(underAChat.length).toBeGreaterThan(0);
    expect(guardedPaths(appSource).sort()).toEqual([...new Set(underAChat)].sort());
  });
});

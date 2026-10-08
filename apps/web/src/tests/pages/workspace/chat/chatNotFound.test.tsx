// An address that does not name a chat.
//
// Two of them, and they have to end in the same place: an id the server cannot
// parse (which must never surface the validator's own words), and an id it CAN
// parse but does not have. Neither draws a chat shell around a chat that does
// not exist.
//
// The rule underneath is the opaque 404: a chat in another org answers exactly
// what a deleted one answers, so whatever this page renders for the one it must
// render, character for character, for the other — otherwise the page is a way
// to ask whether an id exists somewhere the reader cannot see.

import { cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

// The chat composition has its own suite; here it stands in for "the surface
// mounted", which is the mechanism that matters. Every chat-scoped read the
// reader saw fail — the permission mode above all, which is a read of the chat
// row the composer makes on mount — is started by this component. It does not
// mount, they do not happen.
vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: (props: { chatId?: string }) => (
    <div data-testid="chat-surface" data-chat-id={props.chatId ?? ""}>
      <textarea aria-label="Message" />
    </div>
  ),
}));

import { CHAT_GONE_TITLE, ChatPage, ChatRoute } from "@/pages/workspace/chat/ChatPage";

const NOT_FOUND = "This page doesn't exist";
const UUID = "6f1a1c2e-6d41-4e2a-9a77-2b1f0c9d4e55";
const OTHER_ORG_UUID = "0b9c8d7e-1a2b-3c4d-5e6f-708192a3b4c5";

/** The wire, with the chat row answering `status` and everything else fine.
 *
 *  `detail` differs between the two 404 cases on purpose: the server's body is
 *  not what the reader is shown, and a page that leaked it would tell the two
 *  apart. */
function scriptFetch(status: number, detail: string) {
  const spy = vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input);
    const at = new URL(url, "http://x").pathname;
    const json = (body: unknown, code = 200): Response =>
      new Response(JSON.stringify(body), {
        status: code,
        headers: { "content-type": "application/json" },
      });
    if (/^\/api\/v1\/chats\/[^/]+$/.test(at)) return json({ detail }, status);
    if (at.startsWith("/api/v1/chats")) return json({ items: [], next_cursor: null });
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

/** The three chat routes as `App.tsx` declares them — the id one behind its
 *  guard, which is the only place a malformed id can be stopped before the page
 *  it would otherwise mount starts reading. */
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
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("an id that cannot name a chat", () => {
  it("is answered without asking the server anything", async () => {
    const spy = scriptFetch(404, "Not found");
    renderAt("/chat/not-a-uuid");

    expect(await screen.findByText(NOT_FOUND)).toBeTruthy();
    // Not one request: the rail, the drive and the account are reads the chat
    // page makes on mount, and none of them are owed to this address.
    expect(spy).not.toHaveBeenCalled();
  });

  it("never prints the id it refused, or the validator's words", async () => {
    scriptFetch(404, "Input should be a valid UUID, invalid character: found `n` at 1");
    renderAt("/chat/not-a-uuid");

    await screen.findByText(NOT_FOUND);
    const page = document.body.textContent ?? "";
    expect(page).not.toMatch(/not-a-uuid/);
    expect(page).not.toMatch(/UUID/i);
  });
});

describe("an id the server has no chat for", () => {
  it("shows a dead end with nothing of the chat around it", async () => {
    scriptFetch(404, "Not found");
    renderAt(`/chat/${UUID}`);

    expect(await screen.findByText(CHAT_GONE_TITLE)).toBeTruthy();
    // No composer, no files dock, no rail row held open: the whole chat shell
    // is what does not mount, and with it every read it would have made.
    expect(screen.queryByTestId("chat-surface")).toBeNull();
    expect(screen.queryByLabelText("Message")).toBeNull();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(document.body.textContent ?? "").not.toMatch(/mode shown may be stale/i);
    expect(document.body.textContent ?? "").not.toMatch(/Command failed/i);
  });

  it("gets there on the answer, not after the retry ladder", async () => {
    // Retries left ON — the default client's three of them, with the backoff a
    // reader sat through watching a spinner. A refusal is an answer, so the
    // dead end is on screen off the first one, with the ladder never entered.
    const spy = scriptFetch(404, "Not found");
    renderAt(`/chat/${UUID}`);

    await screen.findByText(CHAT_GONE_TITLE);
    // Counted, not timed: a budget shorter than the first backoff is a budget a
    // loaded runner spends on scheduling alone, while one ask on the wire is
    // the ladder not being climbed however slowly the answer arrives.
    const asked = spy.mock.calls.filter(([input]) => {
      const url = input instanceof Request ? input.url : String(input);
      return new URL(url, "http://x").pathname === `/api/v1/chats/${UUID}`;
    });
    expect(asked).toHaveLength(1);
  });

  it("reads identically whether the chat is gone or simply not this reader's", async () => {
    scriptFetch(404, "Not found");
    renderAt(`/chat/${UUID}`);
    await screen.findByText(CHAT_GONE_TITLE);
    const missing = document.body.textContent;

    cleanup();
    vi.unstubAllGlobals();

    // The same opaque 404 a chat in another org answers, with a different body
    // behind it and a different id in front of it.
    scriptFetch(404, "Not found.");
    renderAt(`/chat/${OTHER_ORG_UUID}`);
    await screen.findByText(CHAT_GONE_TITLE);

    expect(document.body.textContent).toBe(missing);
    expect(missing).toContain(CHAT_GONE_TITLE);
  });

  it("still keeps a chat that is merely closed to this reader apart from one that is gone", async () => {
    // The 403 is the case where saying more is the right answer, and it must
    // not have been swept into the dead end with the 404s.
    scriptFetch(403, "Forbidden");
    renderAt(`/chat/${UUID}`);

    expect(await screen.findByText("Ask the owner to share it with you.")).toBeTruthy();
    expect(screen.queryByText(CHAT_GONE_TITLE)).toBeNull();
  });
});

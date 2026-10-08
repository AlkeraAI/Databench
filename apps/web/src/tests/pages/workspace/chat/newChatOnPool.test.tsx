// An organization the shared pool serves can start a chat.
//
// Before a chat exists the page has only the organization's machine read to go
// on. That read answers `pool` when the org's next chat would be placed on a
// shared box — the create places it there at once — and the page must take a
// message on it: reading `pool` as "no machine" left every free, plus and pro
// organization with a dead composer on /chat/new over a create that works. The
// banner is kept for the one case it is true of: nothing would serve the org.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: (props: { unavailable?: boolean; unavailableReason?: string; notice?: unknown }) => (
    <div
      data-testid="chat-surface"
      data-unavailable={props.unavailable ? "yes" : "no"}
      data-reason={props.unavailableReason ?? ""}
    >
      {props.notice as never}
    </div>
  ),
}));

import { ChatPage } from "@/pages/workspace/chat/ChatPage";
import { chatFact } from "../../../fixtures/statusFacts";

const NOTHING_SERVES = "No machine can serve your organization right now.";

/** The org's machine read answering `status`, with no chat of its own open —
 *  or, with `unplaced`, one chat that has not been placed on a box yet. */
function script(status: "pool" | "none", unplaced = false): void {
  const chat = {
    id: "c1",
    title: "Copied chat",
    owner_user_id: "u1",
    machine_id: null,
    machine_status: "none",
    machine_refusal_reason: null,
    created_at: "2026-09-06T12:00:00Z",
    updated_at: "2026-09-06T12:00:00Z",
    last_seq: 0,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const raw = input instanceof Request ? input.url : String(input);
      const at = new URL(raw, "http://x").pathname;
      const json = (payload: unknown): Response =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (at === "/api/v1/machines/current") {
        // A pool box is never named to the tenant: no id, no name.
        return json({
          machine_id: null,
          status,
          name: "",
          reason: "",
          last_heartbeat_at: null,
          // What the server says stands between a chat placed there and its
          // first turn: nothing for a shared box that will take it.
          status_fact: status === "none" ? chatFact("no_machine") : null,
        });
      }
      if (/\/api\/v1\/chats\/[^/?]+$/.test(at)) return json(chat);
      if (at.startsWith("/api/v1/chats")) {
        return json({ items: unplaced ? [chat] : [], next_cursor: null });
      }
      if (at === "/api/v1/auth/me") return json({ id: "u1", email: "dana@example.com" });
      return json({});
    }),
  );
}

function renderAt(path: string): void {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/chat/new" element={<ChatPage />} />
          <Route path="/chat/:chatId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The surface once the org's machine read has answered and every render it
 *  causes has run — re-read from the document, so a surface that was torn down
 *  after its first render is not mistaken for one still standing. */
async function settledSurface(): Promise<HTMLElement> {
  const fetcher = vi.mocked(fetch);
  await waitFor(() =>
    expect(fetcher.mock.calls.some(([input]) => String(input).includes("/api/v1/machines/current"))).toBe(true),
  );
  await new Promise((resolve) => setTimeout(resolve, 30));
  return screen.getByTestId("chat-surface");
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("a new chat for an organization the shared pool serves", () => {
  it("takes a message, with no banner and no reason", async () => {
    script("pool");
    renderAt("/chat/new");
    await screen.findByTestId("chat-surface");
    const surface = await settledSurface();
    expect(surface.dataset.unavailable).toBe("no");
    expect(surface.dataset.reason).toBe("");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("takes a message on a chat not yet placed, too", async () => {
    script("pool", true);
    renderAt("/chat/c1");
    await screen.findByTestId("chat-surface");
    const surface = await settledSurface();
    expect(surface.dataset.unavailable).toBe("no");
    expect(screen.queryByText(NOTHING_SERVES)).toBeNull();
  });
});

describe("a new chat for an organization nothing can serve", () => {
  it("disables the composer and states the fact", async () => {
    script("none");
    renderAt("/chat/new");
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface.dataset.unavailable).toBe("yes"));
    expect(surface.dataset.reason).toBe(NOTHING_SERVES);
    expect(screen.getByRole("status")).toHaveTextContent(NOTHING_SERVES);
    expect(screen.queryByTestId("chat-machine-support")).toBeNull();
  });
});

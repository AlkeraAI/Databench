// A chat that has not been placed on a box is not an organization with no
// workspace.
//
// A duplicated chat is born unbound — it is placed when a machine next reaches
// ready, or on its own first message — and reading that empty binding as the
// chat's machine state told the reader "No workspace is running for your
// organization" while the box serving every other chat they own was answering.
// So an unbound chat reads the machine the ORG is running, because that is the
// box that will take it. The enterprise wording is kept for the one case it is
// true of: an org with no machine at all.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: (props: { unavailable?: boolean; notice?: unknown }) => (
    <div data-testid="chat-surface" data-unavailable={props.unavailable ? "yes" : "no"}>
      {props.notice as never}
    </div>
  ),
}));

import { ChatPage } from "@/pages/workspace/chat/ChatPage";
import type { StatusFact } from "@/api/status";

import { chatFact } from "../../../fixtures/statusFacts";

const NO_WORKSPACE = "No machine can serve your organization right now.";
const STARTING = "lab-b is starting. Your message is sent as soon as it is ready.";

/** A chat with no binding (what a fresh copy looks like), over an org whose
 *  live machine read answers `live`. */
function script(live: { machine_id: string | null; status: string; status_fact?: StatusFact | null }): void {
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
      const chat = {
        id: "c1",
        title: "Yesterday's orders (copy)",
        owner_user_id: "u1",
        machine_id: null,
        machine_status: "none",
        machine_refusal_reason: null,
        created_at: "2026-09-06T12:00:00Z",
        updated_at: "2026-09-06T12:00:00Z",
        last_seq: 3,
      };
      if (at === "/api/v1/machines/current") {
        return json({ ...live, name: "box-1", reason: null });
      }
      if (/\/api\/v1\/chats\/[^/?]+$/.test(at)) return json(chat);
      if (at.startsWith("/api/v1/chats")) return json({ items: [chat], next_cursor: null });
      if (at === "/api/v1/auth/me") return json({ id: "u1", email: "dana@example.com" });
      return json({});
    }),
  );
}

function renderPage(): void {
  const client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("an unplaced chat over a live machine", () => {
  it("says nothing, and takes a message like any other chat", async () => {
    script({ machine_id: "m1", status: "ready" });
    renderPage();
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface.dataset.unavailable).toBe("no"));
    // Settle every read, then make sure the plate never appeared.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText(NO_WORKSPACE)).toBeNull();
  });

  it("reads the box that is coming up, not an organization with no workspace", async () => {
    script({ machine_id: "m1", status: "starting", status_fact: chatFact("starting") });
    renderPage();
    await screen.findByText(STARTING);
    expect(screen.queryByText(NO_WORKSPACE)).toBeNull();
  });

  it("still says so for an organization that has no machine at all", async () => {
    script({ machine_id: null, status: "none", status_fact: chatFact("no_machine") });
    renderPage();
    await screen.findByText(NO_WORKSPACE);
  });
});

// "This page picks it up once it is running" — the banner
// over a chat with no workspace promises that, and this is what makes it true.
//
// A chat opened while the org had no machine is bound to nothing, and the live
// machine's word does not apply to it (it is bound to no machine, so it reads
// its own row: `none`). When the box comes up the backend binds the chat to it
// and announces `chat.updated`; the frame invalidates the chat's own read, the
// refetch carries the binding, and the banner clears — with no reload and no
// second message from the reader. The frame is the whole mechanism here: the
// poll that floors it is fifteen seconds away, well past this test.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "@/api/auth";
import { RealtimeBridge } from "@/api/events/RealtimeBridge";
import { resetRealtimeStatus } from "@/api/events/status";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { chatFact, chatFactForMachine } from "../../../fixtures/statusFacts";
import { openStream, scriptedFetch } from "@/tests/api/events/fakeSse";

vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: (props: { chatId?: string; unavailable?: boolean; notice?: unknown }) => (
    <div data-testid="chat-surface" data-chat-id={props.chatId ?? ""} data-unavailable={props.unavailable ? "yes" : "no"}>
      {props.notice as never}
    </div>
  ),
}));

import { ChatPage } from "@/pages/workspace/chat/ChatPage";

const USER = {
  id: "11111111-1111-1111-1111-111111111111",
  email: "dana@example.com",
  first_name: "Dana",
  last_name: "Reader",
  display_name: "Dana Reader",
  org_team_id: "22222222-2222-2222-2222-222222222222",
  org_name: "Northwind Labs",
  org_role: "member",
  membership_count: 1,
  has_password: true,
  mfa_enabled: false,
  platform_role: null,
  platform_role_display: null,
  email_verified_at: "2026-07-30T00:00:00Z",
  email_verification_required: false,
  email_verification_deadline: null,
  verification_resend_available_at: null,
  created_at: "2026-07-30T00:00:00Z",
} satisfies CurrentUser;

const NO_WORKSPACE = "No machine can serve your organization right now.";

const BASE_CHAT = {
  id: "c1",
  title: "asked while the box was down",
  created_at: "2026-09-15T10:30:00Z",
  updated_at: "2026-09-15T10:30:00Z",
  last_seq: 1,
  machine_refusal_reason: null,
};

/** The chat row as the backend answers it, mutable so the test can bind it. */
let chatRow: Record<string, unknown>;

function scriptApi() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
      if (/\/api\/v1\/chats\/[^/?]+$/.test(at)) return json(chatRow);
      if (at.startsWith("/api/v1/chats")) return json({ items: [chatRow], next_cursor: null });
      if (at.includes("/api/v1/machines/current")) {
        // The org's machine read has not caught up with the box that is coming
        // up — which is exactly the org this banner is written for: nothing
        // live to take the chat, so its empty binding IS "no workspace".
        return json({
          machine_id: null,
          status: "none",
          name: "",
          reason: null,
          last_heartbeat_at: null,
          status_fact: chatFact("no_machine"),
        });
      }
      if (at.includes("/api/v1/auth/me")) return json(USER);
      return json({});
    }),
  );
}

const frame = (id: number, type: string, entityId: string): string =>
  `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify({ type, entity: "chat", entity_id: entityId, version: 1, org_id: USER.org_team_id })}\n\n`;

let script: ReturnType<typeof scriptedFetch>;

beforeEach(() => {
  resetRealtimeStatus();
  chatRow = { ...BASE_CHAT, machine_id: null, machine_status: "none" };
  script = scriptedFetch();
  scriptApi();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mount() {
  const qc = createQueryClient({ retry: false });
  qc.setQueryData(keys.auth.me, USER);
  render(
    <QueryClientProvider client={qc}>
      <RealtimeBridge fetch={script.fetch} />
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return qc;
}

describe("a chat opened while the org had no machine", () => {
  it("drops the banner when the box takes the chat, on the chat.updated frame alone", async () => {
    const stream = openStream();
    script.answerStream(stream);
    mount();

    // The org's box is ready, but the chat is bound to nothing: its own row is
    // what it reads, and the reader is told there is no workspace.
    expect(await screen.findByText(NO_WORKSPACE)).toBeInTheDocument();
    expect(screen.getByTestId("chat-surface").getAttribute("data-unavailable")).toBe("yes");

    // The backend bound the chat to the box. Nothing on this page has been
    // told yet: the row it holds still says none, and it says so.
    chatRow = { ...BASE_CHAT, machine_id: "m1", machine_status: "ready", status: chatFactForMachine("ready") };
    expect(screen.getByText(NO_WORKSPACE)).toBeInTheDocument();

    stream.push(frame(1, "chat.updated", "c1"));

    await waitFor(() => expect(screen.queryByText(NO_WORKSPACE)).toBeNull(), { timeout: 3_000 });
    expect(screen.getByTestId("chat-surface").getAttribute("data-unavailable")).toBe("no");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("keeps the banner on a frame that says nothing about chats", async () => {
    // The control: it is the chat's own event that refreshes the row, not any
    // frame at all. A knowledge-base change lands, the batching window passes
    // and then some, and the page still holds the row it had.
    const stream = openStream();
    script.answerStream(stream);
    mount();
    expect(await screen.findByText(NO_WORKSPACE)).toBeInTheDocument();

    chatRow = { ...BASE_CHAT, machine_id: "m1", machine_status: "ready", status: chatFactForMachine("ready") };
    stream.push(frame(1, "kb_item.changed", "k1"));

    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(screen.getByText(NO_WORKSPACE)).toBeInTheDocument();
  });
});

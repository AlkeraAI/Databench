// Reloading the page before the box has answered.
//
// The bubble a Send puts on screen is held in memory by the store, so a reader
// who refreshed while the box was still waking up came back to a chat with
// their own question missing from it. The durable record is the transcript the
// server writes the instant it accepts the message, and this pins the whole
// way through: the page the server serves → the real fold → the rendered
// transcript.
//
//  1. a fresh mount, reading that page, shows the reader's words, reading as
//     still going out; and
//  2. when the box finally echoes the message back there is exactly ONE of it,
//     and it no longer reads as going out.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const MESSAGE = "Map the warehouse";

const { ds, state } = vi.hoisted(() => {
  const state = { turns: [] as unknown[] };
  const ds = {
    listChats: async () => [{ id: "c1", title: "Warehouse map", updatedAt: "2026-09-16T00:00:00Z" }],
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: vi.fn(async () => state.turns),
    searchFiles: async () => [] as unknown[],
    sendUserMessage: async () => ({ id: "m1", role: "user", content: MESSAGE }),
    getPermissionMode: async () => "read_only",
    setPermissionMode: async () => {},
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
  };
  return { ds, state };
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const host = real.createBrowserChatHost({
    account: () => ({ email: "ben@tideline.example", webAppUrl: null }),
  });
  return {
    ...real,
    chatHost: () => host,
    chatData: () => ds,
    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
    chatCaps: () => ({ opencodeActive: false, modelCatalog: true }),
  };
});

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";
import { useChatStore } from "@/pages/workspace/chat/chatStore";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import type { DocHandle } from "@/api/realtime/docSync";

const AT = "2026-09-16T12:00:00Z";

/** The row the server writes for a person's message — flat, no `event_type`. */
const PROMPT_ROW = {
  id: "row-1",
  chat_id: "c1",
  seq: 1,
  role: "user" as const,
  kind: "prompt",
  event_id: "usr:web-1",
  payload: {
    schema_version: "1.2.0",
    kind: "prompt",
    text: MESSAGE,
    client_id: "web-1",
    user_id: "u1",
    attachments: [],
    context: "",
  },
  created_at: AT,
};

/** The two events the harness publishes when it takes the message, as the
 *  server stores them: the envelope, with the event one level in. */
const ECHO_ROWS = [
  { event_id: "hm1-created", event_type: "message.created", message_id: "hm1", role: "user" },
  {
    event_id: "hm1-part",
    event_type: "part.created",
    message_id: "hm1",
    part: { part_id: "hm1-t", message_id: "hm1", type: "text", text: MESSAGE },
  },
].map((payload, i) => ({
  id: `row-${i + 2}`,
  chat_id: "c1",
  seq: i + 2,
  role: "user" as const,
  kind: payload.event_type,
  event_id: payload.event_id,
  payload: { event_id: payload.event_id, role: "user", kind: payload.event_type, payload },
  created_at: AT,
}));

function idleDoc(): DocHandle<never> {
  return {
    onMessage: () => () => undefined,
    onPhase: () => () => undefined,
    getPhase: () => ({
      phase: "live",
      epoch: 1,
      seq: 0,
      peerId: "p:1",
      canWrite: false,
      pending: 0,
      error: null,
    }),
    sendOp: () => Promise.reject(new Error("a reader never writes to a chat document")),
    dispose: () => undefined,
  } as unknown as DocHandle<never>;
}

/** The turns a browser that just loaded this chat derives from the server's own
 *  page — the REAL source and the REAL fold, not turns written by hand. */
async function turnsFromServerPage(items: unknown[]): Promise<unknown[]> {
  const listMessages = vi
    .fn()
    .mockResolvedValueOnce({ items, next_after_seq: 99, resync_from: null })
    .mockResolvedValue({ items: [], next_after_seq: 99, resync_from: null });
  const source = new CloudDataSource({
    rest: { listMessages } as never,
    openDoc: () => idleDoc(),
    acquire: () => () => undefined,
    clientId: () => "web-1",
  });
  return source.getChatTurns("c1");
}

afterEach(() => {
  cleanup();
  state.turns = [];
  vi.clearAllMocks();
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
});

function mountChat() {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatSurface chatId="c1" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("a reload before anything has answered", () => {
  it("shows the message the reader sent, with nothing under it", async () => {
    state.turns = await turnsFromServerPage([PROMPT_ROW]);

    const { container } = mountChat();

    expect(await screen.findByText(MESSAGE)).toBeTruthy();
    expect(
      screen.queryByText("Sending…"),
      "a message that is simply sent carries no word for the wait",
    ).toBeNull();
    expect(
      container.querySelector(".chat-ticket__at"),
      "nor a stamp naming a time the turn never started at",
    ).toBeNull();
  });

  it("shows it ONCE, stamped, once the box echoes the same words back", async () => {
    state.turns = await turnsFromServerPage([PROMPT_ROW, ...ECHO_ROWS]);

    const { container } = mountChat();

    await waitFor(() => expect(screen.getAllByText(MESSAGE).length).toBe(1));
    await waitFor(() =>
      expect(
        container.querySelector(".chat-ticket__at"),
        "a box took it, so the message is stamped with the time it was said",
      ).toBeTruthy(),
    );
  });
});

// Sending to an EXISTING chat on the shipping surface. The working display
// goes up on Send and holds for the whole turn (it is derived from the
// transcript, not flicked off when the previous turn reads done), the chat list
// refreshes so every surface's freshness stays true, a refused send reads under
// the message it belongs to, and Stop ends the turn.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../../api/queryClient";

const USER = { id: "u0", author: "user", status: "done", parts: [{ id: "u0-t", kind: "text", text: "Hi" }] };
const ASSISTANT_DONE = {
  id: "a0",
  author: "assistant",
  status: "done",
  completedAt: "2026-06-10T00:00:01Z",
  parts: [{ id: "a0-t", kind: "text", text: "Hi! How can I help?" }],
};
const USER_NEW = { id: "u1", author: "user", status: "done", parts: [{ id: "u1-t", kind: "text", text: "Hi again" }] };
// What the data source folds when the daemon rejects a send: the user's
// message, THEN the error turn.
const SEND_ERROR = {
  id: "e0",
  author: "assistant",
  status: "error",
  parts: [{ id: "e0-t", kind: "system", text: "The agent ran into a problem.", tone: "error" }],
};

const { ds, state, engineRequest } = vi.hoisted(() => {
  // The mutable transcript the mocked getChatTurns reads, so a test can advance
  // the conversation the way the daemon would.
  const state = { turns: [] as unknown[] };
  const engineRequest = vi.fn(async () => ({}));
  const ds = {
    listChats: vi.fn(async () => [{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }]),
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: vi.fn(async () => state.turns),
    searchFiles: async () => [] as unknown[],
    sendUserMessage: vi.fn(async () => ({ id: "m1", role: "user", content: "Hi again" })),
    createChat: vi.fn(),
    subscribeChat: () => () => {},
    subscribePermissionMode: () => () => {},
    getPermissionMode: async () => "default",
    cancelTurn: vi.fn(async () => ({ stopped: true })),
  };
  return { ds, state, engineRequest };
});

// Partial mock: only the data source and the polling knobs are substituted, so
// the store's own reads off this barrel (isSessionNotOpen on a rejected send)
// stay the real ones.
vi.mock("./data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./data")>()),
  chatHost: () => ({
    kind: "vscode",
    engine: { request: engineRequest },
    runCommand: async () => {},
    openFile: async () => {},
    subscribe: () => () => {},
    workspacePath: () => null,
    account: () => ({ email: null, webAppUrl: null }),
    onAccountChange: () => () => {},
    auth: { openBrowser: async () => {}, getState: () => undefined, setState: () => {}, on: () => () => {}, ready: () => {} },
  }),
  chatData: () => ds,
  refetchWhileErrored: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
  chatCaps: () => ({ opencodeActive: false }),
}));

import { ChatSurface } from "./ChatSurface";
import { useChatStore } from "./chatStore";

afterEach(() => {
  cleanup();
  state.turns = [];
  vi.clearAllMocks();
  ds.listChats.mockResolvedValue([{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }]);
  ds.sendUserMessage.mockResolvedValue({ id: "m1", role: "user", content: "Hi again" });
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

function renderChat() {
  const qc = createQueryClient({ retry: false });
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/chat/c1"]}>
          <Routes>
            <Route path="/chat/:id" element={<ChatSurface />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

const working = () => screen.queryByText(/^working/i);

async function sendHiAgain(): Promise<void> {
  const box = screen.getByRole("textbox", { name: /^message /i });
  await act(async () => {
    fireEvent.change(box, { target: { value: "Hi again" } });
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
  });
}

describe("ChatSurface send on an existing chat", () => {
  it("keeps the working display up from Send until the turn finishes", async () => {
    state.turns = [USER, ASSISTANT_DONE]; // a finished exchange
    renderChat();

    await waitFor(() => expect(screen.getByText("Hi! How can I help?")).toBeInTheDocument());
    expect(working()).toBeNull();

    // The transcript now ends with the new user message (the agent's turn is
    // pending), so the display shows and STAYS.
    state.turns = [USER, ASSISTANT_DONE, USER_NEW];
    await sendHiAgain();

    await waitFor(() => expect(working()).toBeInTheDocument());
    await act(async () => {
      await Promise.resolve();
    });
    expect(working()).toBeInTheDocument();
  });

  it("re-reads the chat list after a send so its freshness is current", async () => {
    state.turns = [USER, ASSISTANT_DONE];
    renderChat();
    await waitFor(() => expect(screen.getByText("Hi! How can I help?")).toBeInTheDocument());
    const before = ds.listChats.mock.calls.length;

    await sendHiAgain();

    await waitFor(() => expect(ds.listChats.mock.calls.length).toBeGreaterThan(before));
  });

  it("renders a refused send as the message, then the error", async () => {
    state.turns = [USER, ASSISTANT_DONE];
    ds.sendUserMessage.mockRejectedValueOnce(new Error("the daemon refused the send"));
    renderChat();
    await waitFor(() => expect(screen.getByText("Hi! How can I help?")).toBeInTheDocument());

    // The daemon refused, so the data source folds the user's message FIRST and
    // the error after it.
    state.turns = [USER, ASSISTANT_DONE, USER_NEW, SEND_ERROR];
    await sendHiAgain();

    await waitFor(() => expect(screen.getByText("The agent ran into a problem.")).toBeInTheDocument());
    // One "Hi again": the echoed user turn retired the optimistic bubble
    // instead of doubling it.
    expect(screen.getAllByText("Hi again")).toHaveLength(1);
    const message = screen.getByText("Hi again");
    const error = screen.getByText("The agent ran into a problem.");
    expect(message.compareDocumentPosition(error) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    // The turn ended, so the working display clears.
    await waitFor(() => expect(working()).toBeNull());
  });

  it("ends a running turn from the composer's Stop", async () => {
    state.turns = [USER, ASSISTANT_DONE];
    renderChat();
    await waitFor(() => expect(screen.getByText("Hi! How can I help?")).toBeInTheDocument());

    state.turns = [USER, ASSISTANT_DONE, USER_NEW];
    await sendHiAgain();
    const stop = await screen.findByRole("button", { name: "Stop" });

    await act(async () => {
      fireEvent.click(stop);
    });

    // Stopping goes through the SOURCE, which is what each shell implements:
    // the editor on the daemon's engine channel, the portal towards the machine.
    // The surface never reaches for a host channel of its own.
    expect(ds.cancelTurn).toHaveBeenCalledWith("c1");
    expect(engineRequest).not.toHaveBeenCalledWith("chat.cancel", { chatId: "c1" });
    await waitFor(() => expect(working()).toBeNull());
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
  });
});

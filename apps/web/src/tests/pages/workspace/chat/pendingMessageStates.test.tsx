// A message sent while the workspace is not up says it is waiting for it, not
// that the agent is working, until the box takes it and its turn starts. A
// send refused because the workspace stopped answering keeps the words.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";

/** The send goes over the wire, so the refusal is a real 403 response parsed by
 *  the real error type — not a hand-made rejection shaped to match. */
const { ds, state } = vi.hoisted(() => {
  const state = {
    turns: [] as unknown[],
    turnState: "idle" as string,
    listeners: new Set<(event: { replay: boolean }) => void>(),
  };
  const ds = {
    listChats: async () => [{ id: "c1", title: "Chat", updatedAt: "2026-09-06T12:00:00Z" }],
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: async () => state.turns,
    searchFiles: async () => [] as unknown[],
    sendUserMessage: async (chatId: string, content: string) => {
      const response = await fetch(`/api/v1/chats/${chatId}/messages`, {
        method: "POST",
        body: JSON.stringify({ content }),
      });
      if (!response.ok) throw new ApiError(response.status, await response.json());
      return { id: "m1", role: "user", content };
    },
    getPermissionMode: async () => "read_only",
    setPermissionMode: async () => {},
    subscribePermissionMode: () => () => {},
    subscribeChat: (_chatId: string, listener: (event: { replay: boolean }) => void) => {
      state.listeners.add(listener);
      return () => state.listeners.delete(listener);
    },
    turnState: () => state.turnState,
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

import { ChatSurface, WAITING_FOR_MACHINE } from "@/pages/workspace/chat/ChatSurface";
import { useChatStore } from "@/pages/workspace/chat/chatStore";

/** How the refused POST answers: 403 with the backend's envelope, or 500 for
 *  the fault that is NOT a refusal. */
let sendStatus = 201;
let sendBody: unknown = { id: "m1", role: "user", content: "" };

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = input instanceof Request ? input.url : String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      const answer = (status: number, payload: unknown): Response =>
        new Response(JSON.stringify(payload), {
          status,
          headers: { "content-type": "application/json" },
        });
      if (method === "POST" && raw.includes("/messages")) return answer(sendStatus, sendBody);
      return answer(200, {});
    }),
  );
}

function renderChat(machineWaiting = false) {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route
            path="/chat/:id"
            element={<ChatSurface chatId="c1" machineWaiting={machineWaiting} />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function send(text: string): Promise<void> {
  const box = screen.getByRole("textbox", { name: /^message /i });
  await act(async () => {
    fireEvent.change(box, { target: { value: text } });
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
  });
}

beforeEach(() => {
  sendStatus = 201;
  sendBody = { id: "m1", role: "user", content: "" };
  state.turns = [];
  state.turnState = "idle";
  state.listeners.clear();
  useChatStore.setState({ byId: {}, composerPrefs: {} });
  stubApi();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function turnStarts(): Promise<void> {
  state.turnState = "working";
  await act(async () => {
    for (const listener of state.listeners) listener({ replay: false });
    await Promise.resolve();
  });
}

describe("a message sent while the workspace is not up", () => {
  it("waits for the workspace until its turn starts, then works", async () => {
    renderChat(true);
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());

    await send("are you back?");

    expect(await screen.findByText(WAITING_FOR_MACHINE)).toBeInTheDocument();
    expect(screen.queryByText("Working…")).toBeNull();
    await turnStarts();
    expect(await screen.findByText("Working…")).toBeInTheDocument();
    expect(screen.queryByText(WAITING_FOR_MACHINE)).toBeNull();
  });

  it("says working at once when the workspace is up", async () => {
    renderChat(false);
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());

    await send("hello");

    expect(await screen.findByText("Working…")).toBeInTheDocument();
    expect(screen.queryByText(WAITING_FOR_MACHINE)).toBeNull();
  });
});

describe("a send refused because the workspace stopped answering", () => {
  it("says the server's sentence and keeps the message for a retry", async () => {
    const said = "This chat's workspace machine stopped answering. Start it again to resume the chat, then send the message.";
    sendStatus = 409;
    sendBody = { error: { code: "machine_unreachable", message: said } };
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());

    await send("try this again");

    expect(await screen.findByText(said)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: /^message /i })).toHaveValue("try this again"),
    );
    await waitFor(() => expect(screen.queryByText("Working…")).toBeNull());
  });
});

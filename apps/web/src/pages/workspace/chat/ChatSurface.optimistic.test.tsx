// First paint after Send. The message goes up before anything comes back --
// on a brand-new chat, where createChat is still opening the harness, and on an
// existing one, where the daemon has not echoed the turn yet. The reader never
// watches a spinner wondering whether their message was taken.

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
/** What the host rejects a create with when it cannot open a chat. */
const REFUSED = new Error("the fake host would not open a chat");

const { ds, state } = vi.hoisted(() => {
  const state = { chats: [] as Record<string, unknown>[], turns: [] as unknown[], createCalls: [] as string[] };
  const ds = {
    listChats: vi.fn(async () => state.chats),
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: vi.fn(async () => state.turns),
    searchFiles: async () => [] as unknown[],
    // Never resolves by default: the just-sent turn must paint without it.
    sendUserMessage: vi.fn(() => new Promise<never>(() => {})),
    getPermissionMode: async () => "default",
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    // Never resolves either -- this is the slow harness open.
    createChat: vi.fn((text: string) => {
      state.createCalls.push(text);
      return new Promise<never>(() => {});
    }),
  };
  return { ds, state };
});

vi.mock("./data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./data")>()),
  chatHost: () => ({
    kind: "vscode",
    engine: { request: async () => ({}) },
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
import { DEFAULT_PROMPTS, createErrorTurn } from "./controller";

/** What a failed create reads as, taken from the formatter the surface uses. */
function failureLine(rejection: unknown): string {
  const part = createErrorTurn(rejection).parts[0];
  if (part.kind !== "system") throw new Error("a failed create no longer reads as a system line");
  return part.text;
}

afterEach(() => {
  cleanup();
  state.chats = [];
  state.turns = [];
  state.createCalls = [];
  vi.clearAllMocks();
  ds.createChat.mockImplementation((text: string) => {
    state.createCalls.push(text);
    return new Promise<never>(() => {});
  });
  ds.sendUserMessage.mockImplementation(() => new Promise<never>(() => {}));
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

function renderChat(entry: string) {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/chat" element={<ChatSurface />} />
          <Route path="/chat/:id" element={<ChatSurface />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const working = () => screen.queryByText(/^working/i);
/** The empty transcript's first suggestion, named the way the reader sees it. */
const firstSuggestion = () => screen.queryByRole("button", { name: new RegExp(DEFAULT_PROMPTS[0].label, "i") });

async function send(text: string): Promise<void> {
  const box = screen.getByRole("textbox", { name: /^message /i });
  await act(async () => {
    fireEvent.change(box, { target: { value: text } });
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
  });
}

describe("ChatSurface first send with no chat yet", () => {
  it("shows the message while the harness is still opening", async () => {
    renderChat("/chat");
    await waitFor(() => expect(firstSuggestion()).toBeInTheDocument());

    await send("Hello");

    // createChat is still pending, and the message is already on screen.
    expect(state.createCalls).toEqual(["Hello"]);
    expect(screen.getByText("Hello")).toBeInTheDocument();
    // No flash back to the empty screen, and the turn reads as running.
    expect(firstSuggestion()).toBeNull();
    expect(working()).toBeInTheDocument();
  });

  it("keeps the message and stops the turn when the chat cannot be opened", async () => {
    ds.createChat.mockImplementationOnce(() => Promise.reject(REFUSED));
    renderChat("/chat");
    await waitFor(() => expect(firstSuggestion()).toBeInTheDocument());

    await send("Hello");

    // The message stays where it was sent, under the reason it could not go…
    const bubbles = screen.getAllByText("Hello").filter((node) => node.tagName !== "TEXTAREA");
    expect(bubbles).toHaveLength(1);
    expect(screen.getByText(failureLine(REFUSED))).toBeInTheDocument();
    await waitFor(() => expect(working()).toBeNull());
    // …and the words are handed back to the composer, because a chat that was
    // never started is a message the reader still has to send.
    expect(screen.getByRole("textbox", { name: /^message /i })).toHaveValue("Hello");
  });
});

describe("ChatSurface send on an open chat", () => {
  it("shows the turn before the daemon confirms it", async () => {
    state.chats = [{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }];
    state.turns = [USER, ASSISTANT_DONE];
    renderChat("/chat/c1");
    await waitFor(() => expect(screen.getByText("Hi! How can I help?")).toBeInTheDocument());

    await send("Map the warehouse");

    // sendUserMessage is still in flight and the folded transcript has not
    // echoed the turn, yet both the message and the working display are up.
    expect(ds.sendUserMessage).toHaveBeenCalledWith("c1", "Map the warehouse", { clientId: expect.any(String) });
    expect(screen.getByText("Map the warehouse")).toBeInTheDocument();
    expect(working()).toBeInTheDocument();
    // The composer is clear for the next message.
    expect(screen.getByRole("textbox", { name: /^message /i })).toHaveValue("");
  });
});

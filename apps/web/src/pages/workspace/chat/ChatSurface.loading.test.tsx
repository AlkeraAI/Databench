// What fills the tape before there is a transcript.
//
// Opening an EXISTING chat waits on the daemon (~2s to spawn the harness). That
// wait is not the agent thinking and it is not an empty chat, so neither the
// working indicator nor the opening suggestions may appear during it — a chat
// with history must not offer to start a conversation it already has. A chat
// with nothing in it does offer them, and picking one starts the chat.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const { ds, state } = vi.hoisted(() => {
  const state = {
    chats: [] as Record<string, unknown>[],
    // Held open while the daemon opens the chat; a test resolves it to land the
    // replay.
    turns: null as { promise: Promise<unknown[]>; resolve: (turns: unknown[]) => void } | null,
  };
  const ds = {
    listChats: async () => state.chats,
    listModels: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: async () => [] as unknown[],
    listContext: async () => ({ total: 0, items: [] as unknown[] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] as unknown[] }),
    getChatTurns: vi.fn(() => state.turns?.promise ?? Promise.resolve([] as unknown[])),
    searchFiles: async () => [] as unknown[],
    subscribeChat: () => () => {},
    subscribePermissionMode: () => () => {},
    getPermissionMode: async () => "default",
    setPermissionMode: vi.fn(async () => {}),
    setEffort: vi.fn(async () => {}),
    // Never resolves: the new chat stays in its creating state so the first
    // paint after a pick is assertable without a navigation race.
    createChat: vi.fn(() => new Promise<never>(() => {})),
    sendUserMessage: vi.fn(async () => ({})),
  };
  return { ds, state };
});

vi.mock("./data", () => ({
  chatHost: () => ({
    engine: { request: async () => ({}) },
    runCommand: async () => {},
    openFile: async () => {},
    subscribe: () => () => {},
    workspacePath: () => null,
    account: () => ({ email: null, webAppUrl: null }),
    onAccountChange: () => () => {},
    auth: {
      openBrowser: async () => {},
      getState: () => undefined,
      setState: () => {},
      on: () => () => {},
      ready: () => {},
    },
  }),

  chatData: () => ds,
  refetchWhileErrored: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
  chatCaps: () => ({ opencodeActive: true }),
  isSessionNotOpen: () => false,
  errorText: (err: unknown) =>
    typeof err === "object" && err !== null && typeof (err as { message?: unknown }).message === "string"
      ? (err as { message: string }).message
      : String(err),
  exportBlob: async () => {},
}));

import { ChatSurface } from "./ChatSurface";
import { useChatStore } from "./chatStore";
import { DEFAULT_PROMPTS } from "./controller";

const REPLY = "Here is the lineage.";
const FIRST_PROMPT = DEFAULT_PROMPTS[0];

/** The opening suggestions, addressed by the prompt text the surface offers. */
const suggestion = (label: string): HTMLElement | null =>
  screen.queryByRole("button", { name: new RegExp(label, "i") });

function deferredTurns(): void {
  let resolve!: (turns: unknown[]) => void;
  const promise = new Promise<unknown[]>((res) => {
    resolve = res;
  });
  state.turns = { promise, resolve };
}

function renderAt(entry: string) {
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

afterEach(() => {
  cleanup();
  state.chats = [];
  state.turns = null;
  vi.clearAllMocks();
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

describe("ChatSurface opening an existing chat", () => {
  it("offers neither the working indicator nor the opening suggestions while history loads", async () => {
    state.chats = [{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }];
    deferredTurns();
    renderAt("/chat/c1");

    await waitFor(() => expect(ds.getChatTurns).toHaveBeenCalledWith("c1"));
    // Nothing is thinking: history is being fetched.
    expect(screen.queryByText(/working/i)).not.toBeInTheDocument();
    // And this chat is not empty, so it must not offer to start one.
    expect(suggestion(FIRST_PROMPT.label)).toBeNull();
  });

  it("shows the replayed transcript once the daemon answers", async () => {
    state.chats = [{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }];
    deferredTurns();
    renderAt("/chat/c1");
    await waitFor(() => expect(ds.getChatTurns).toHaveBeenCalledWith("c1"));

    await act(async () => {
      state.turns?.resolve([
        { id: "a1", author: "assistant", status: "done", parts: [{ id: "a1-t", kind: "text", text: REPLY }] },
      ]);
      await state.turns?.promise;
    });

    expect(await screen.findByText(REPLY)).toBeInTheDocument();
    expect(suggestion(FIRST_PROMPT.label)).toBeNull();
  });
});

describe("ChatSurface with nothing to show yet", () => {
  it("offers every opening suggestion on a chat that has not started", async () => {
    renderAt("/chat");

    for (const prompt of DEFAULT_PROMPTS) {
      expect(await screen.findByRole("button", { name: new RegExp(prompt.label, "i") })).toBeInTheDocument();
    }
    expect(document.querySelector(".chat-tape")).toHaveAttribute("data-empty");
  });

  it("starts the chat with the suggestion's own text, not its label", async () => {
    renderAt("/chat");
    const pick = await screen.findByRole("button", { name: new RegExp(FIRST_PROMPT.label, "i") });

    await act(async () => {
      fireEvent.click(pick);
    });

    // The label is the short offer; the prefill is the message the agent gets.
    // It goes through the composer's own send, so it carries the reader's picks
    // the way a typed first message does.
    await waitFor(() => expect(ds.createChat).toHaveBeenCalledTimes(1));
    expect((ds.createChat.mock.calls[0] as unknown[])[0]).toBe(FIRST_PROMPT.prefill);
    expect(await screen.findByText(FIRST_PROMPT.prefill)).toBeInTheDocument();
  });
});

// A chat whose transcript could not be read is not an empty chat. Under a
// database stall the read answered 503 and an existing conversation was drawn
// as a brand-new one, suggestions and all, which reads as the chat having been
// deleted.
describe("ChatSurface when the transcript cannot be read", () => {
  const unavailable = (): Error & { status: number } =>
    Object.assign(new Error("The database is busy"), { status: 503 });
  const answered = () =>
    Promise.resolve([
      { id: "a1", author: "assistant", status: "done", parts: [{ id: "a1-t", kind: "text", text: REPLY }] },
    ]);

  it("says it could not load, offers a retry and never the opening suggestions", async () => {
    state.chats = [{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }];
    ds.getChatTurns.mockImplementationOnce(() => Promise.reject(unavailable()));
    renderAt("/chat/c1");

    expect(await screen.findByText("This chat's messages could not be loaded.")).toBeInTheDocument();
    for (const prompt of DEFAULT_PROMPTS) expect(suggestion(prompt.label)).toBeNull();

    ds.getChatTurns.mockImplementationOnce(answered);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    });

    expect(await screen.findByText(REPLY)).toBeInTheDocument();
    expect(screen.queryByText("This chat's messages could not be loaded.")).toBeNull();
  });

  it("reads it again on its own when the browser comes back online", async () => {
    state.chats = [{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }];
    ds.getChatTurns.mockImplementationOnce(() => Promise.reject(unavailable()));
    renderAt("/chat/c1");
    expect(await screen.findByText("This chat's messages could not be loaded.")).toBeInTheDocument();

    ds.getChatTurns.mockImplementationOnce(answered);
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });
    expect(await screen.findByText(REPLY)).toBeInTheDocument();
  });

  it("still offers the suggestions on a chat that loaded and has no messages", async () => {
    state.chats = [{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }];
    renderAt("/chat/c1");
    expect(await screen.findByRole("button", { name: new RegExp(FIRST_PROMPT.label, "i") })).toBeInTheDocument();
    expect(screen.queryByText("This chat's messages could not be loaded.")).toBeNull();
  });
});

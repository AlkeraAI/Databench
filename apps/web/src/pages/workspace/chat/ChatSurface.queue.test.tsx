// Enter during a running turn, on the shipping surface.
//
// The reader asks the next thing before the agent has finished answering the
// last one. The keypress is not swallowed: the message is held under the
// composer where they can read it, change it or take it back, and it goes out
// once — through the same send path a typed message takes — when the turn ends.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { accountKey } from "@alkera/ui/storage";

import { createQueryClient } from "../../../api/queryClient";

const USER = {
  id: "u0",
  author: "user",
  status: "done",
  parts: [{ id: "u0-t", kind: "text", text: "Hi" }],
};
const ASSISTANT_DONE = {
  id: "a0",
  author: "assistant",
  status: "done",
  completedAt: "2026-06-10T00:00:01Z",
  parts: [{ id: "a0-t", kind: "text", text: "Hi! How can I help?" }],
};
const USER_NEW = {
  id: "u1",
  author: "user",
  status: "done",
  parts: [{ id: "u1-t", kind: "text", text: "Hi again" }],
};
/** The agent's answer, still streaming. */
const ASSISTANT_LIVE = {
  id: "a1",
  author: "assistant",
  status: "done",
  parts: [{ id: "a1-t", kind: "text", text: "Working on it" }],
};
const ASSISTANT_SETTLED = {
  ...ASSISTANT_LIVE,
  completedAt: "2026-06-10T00:00:09Z",
};

const { ds, state, subscribers } = vi.hoisted(() => {
  const state = { turns: [] as unknown[], turnState: null as "working" | "idle" | null };
  const subscribers: Array<(event: { replay?: boolean }) => void> = [];
  const ds = {
    listChats: vi.fn(async () => [{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }]),
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: vi.fn(async () => state.turns),
    turnState: vi.fn(() => state.turnState),
    searchFiles: async () => [] as unknown[],
    sendUserMessage: vi.fn(async (_chatId: string, _text: string, _opts?: unknown) => ({
      id: "m1",
      role: "user",
      content: "x",
    })),
    createChat: vi.fn(),
    subscribeChat: (_id: string, cb: (event: { replay?: boolean }) => void) => {
      subscribers.push(cb);
      return () => {
        const i = subscribers.indexOf(cb);
        if (i >= 0) subscribers.splice(i, 1);
      };
    },
    subscribePermissionMode: () => () => {},
    getPermissionMode: async () => "default",
    cancelTurn: vi.fn(async () => ({ stopped: true })),
  };
  return { ds, state, subscribers };
});

vi.mock("./data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./data")>()),
  chatHost: () => ({
    kind: "vscode",
    engine: { request: vi.fn(async () => ({})) },
    runCommand: async () => {},
    openFile: async () => {},
    subscribe: () => () => {},
    workspacePath: () => null,
    // The editor names only the email: its queue is kept under the email with
    // no org.
    account: () => ({ email: "dana@example.com", webAppUrl: null }),
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
  chatCaps: () => ({ opencodeActive: false }),
}));

import { ChatSurface } from "./ChatSurface";
import { useChatStore } from "./chatStore";

afterEach(() => {
  cleanup();
  state.turns = [];
  state.turnState = null;
  subscribers.length = 0;
  vi.clearAllMocks();
  ds.listChats.mockResolvedValue([{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }]);
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
  try {
    localStorage.clear();
  } catch {
    // A browser that refuses the store leaves nothing to clear.
  }
});

function renderChat() {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:id" element={<ChatSurface />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const field = (): HTMLElement => screen.getByRole("textbox", { name: "Message Databench" });

async function type(text: string): Promise<void> {
  await act(async () => {
    fireEvent.change(field(), { target: { value: text } });
  });
  await act(async () => {
    fireEvent.keyDown(field(), { key: "Enter" });
  });
}

/** Advance the conversation the way the daemon's stream would. */
async function daemon(turns: unknown[], turnState: "working" | "idle"): Promise<void> {
  state.turns = turns;
  state.turnState = turnState;
  await act(async () => {
    subscribers.forEach((cb) => cb({ replay: false }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("a message typed while the agent is working", () => {
  it("is held under the composer and sent once the turn ends", async () => {
    state.turns = [USER, ASSISTANT_DONE];
    renderChat();
    await waitFor(() => expect(screen.getByText("Hi! How can I help?")).toBeInTheDocument());

    // The reader's first message starts the turn.
    state.turns = [USER, ASSISTANT_DONE, USER_NEW];
    await type("Hi again");
    await daemon([USER, ASSISTANT_DONE, USER_NEW, ASSISTANT_LIVE], "working");
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    ds.sendUserMessage.mockClear();

    // The next question, typed mid-answer.
    await type("and then deploy it");
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(screen.getByText("Queued · sends when the turn finishes")).toBeInTheDocument();
    expect(screen.getByDisplayValue("and then deploy it")).toBeInTheDocument();
    // The field is clear: the words are on screen below it, not lost.
    expect((field() as HTMLTextAreaElement).value).toBe("");

    // The turn ends.
    await daemon([USER, ASSISTANT_DONE, USER_NEW, ASSISTANT_SETTLED], "idle");
    await waitFor(() => expect(ds.sendUserMessage).toHaveBeenCalledTimes(1));
    expect(ds.sendUserMessage.mock.calls[0]?.[1]).toBe("and then deploy it");
    expect(screen.queryByText("Queued · sends when the turn finishes")).not.toBeInTheDocument();
  });

  it("sends nothing for a held message the reader takes back", async () => {
    state.turns = [USER, ASSISTANT_DONE];
    renderChat();
    await waitFor(() => expect(screen.getByText("Hi! How can I help?")).toBeInTheDocument());

    state.turns = [USER, ASSISTANT_DONE, USER_NEW];
    await type("Hi again");
    await daemon([USER, ASSISTANT_DONE, USER_NEW, ASSISTANT_LIVE], "working");
    ds.sendUserMessage.mockClear();

    await type("never mind");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove queued message" }));
    });
    expect(screen.queryByText("Queued · sends when the turn finishes")).not.toBeInTheDocument();

    await daemon([USER, ASSISTANT_DONE, USER_NEW, ASSISTANT_SETTLED], "idle");
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
  });

  it("opening the chat onto a queue left by a previous session sends nothing", async () => {
    // The whole point: the reader is not here to see it happen. Opening a chat
    // must never put words in it by itself.
    localStorage.setItem(
      `${accountKey("dana@example.com", "", "chat.queued")}:c1`,
      JSON.stringify([{ id: "q-before", text: "and then deploy it" }]),
    );
    state.turns = [USER, ASSISTANT_DONE];
    state.turnState = "idle";
    renderChat();
    await waitFor(() => expect(screen.getByText("Hi! How can I help?")).toBeInTheDocument());

    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(screen.getByText("Queued · not sent")).toBeInTheDocument();
    expect(screen.getByDisplayValue("and then deploy it")).toBeInTheDocument();

    // It goes only when the reader says so — and then exactly once.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send queued message" }));
    });
    await waitFor(() => expect(ds.sendUserMessage).toHaveBeenCalledTimes(1));
    expect(ds.sendUserMessage.mock.calls[0]?.[1]).toBe("and then deploy it");
    expect(screen.queryByText("Queued · not sent")).not.toBeInTheDocument();
  });

  it("keeps the composer busy while the machine works through a settled transcript", async () => {
    state.turns = [USER, ASSISTANT_DONE];
    renderChat();
    await waitFor(() => expect(screen.getByText("Hi! How can I help?")).toBeInTheDocument());

    state.turns = [USER, ASSISTANT_DONE, USER_NEW];
    await type("Hi again");
    await daemon([USER, ASSISTANT_DONE, USER_NEW, ASSISTANT_LIVE], "working");
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();

    // A compaction settles the answer message mid-turn; the box is still
    // working and re-sends the same prompt after it. The key must not flip
    // back to Send and then away again.
    await daemon([USER, ASSISTANT_DONE, USER_NEW, ASSISTANT_SETTLED], "working");
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
  });
});

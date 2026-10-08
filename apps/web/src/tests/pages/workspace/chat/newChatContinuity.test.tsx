// Pressing Send on the chats screen shows the chat at once and KEEPS showing
// it across the hop onto the created chat's own route.
//
// The first message crosses the hop in a hand-off slot the created chat adopts
// on open. Two things must not go blank: the surface must not remount on the
// hop (a fresh instance, one frame of the empty screen, a new composer), and
// under React's development double-invoke of effects the FIRST open must not
// spend the slot the SECOND (the one that lasts) needs. This file pins both, through the real `ChatSurface`,
// the real store and the real router, with `createChat` parked on a promise
// and a box that has published nothing.

import { StrictMode, type ReactNode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const MESSAGE = "Map the warehouse";

/** The echoed user turn, as the box publishes it back. */
const ECHO = {
  id: "u1",
  author: "user",
  status: "done",
  parts: [{ id: "u1-t", kind: "text", text: MESSAGE }],
};

const { ds, state } = vi.hoisted(() => {
  const state = {
    chats: [] as Record<string, unknown>[],
    turns: [] as unknown[],
    releaseCreate: () => {},
    createRejection: null as unknown,
    listeners: [] as ((event: { replay?: boolean }) => void)[],
  };
  const ds = {
    listChats: async () => state.chats,
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
    subscribeChat: (_chatId: string, onEvent: (event: { replay?: boolean }) => void) => {
      state.listeners.push(onEvent);
      return () => {
        state.listeners = state.listeners.filter((entry) => entry !== onEvent);
      };
    },
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
    createChat: vi.fn(async () => {
      await new Promise<void>((resolve) => {
        state.releaseCreate = resolve;
      });
      if (state.createRejection) throw state.createRejection;
      // Titled differently from the message so a count of the bubble never
      // counts the heading.
      const chat = { id: "c1", title: "Warehouse map", updatedAt: "2026-09-15T00:00:00Z" };
      state.chats = [chat];
      return chat;
    }),
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
import { optimisticUserTurn, useChatStore } from "@/pages/workspace/chat/chatStore";
import { useSurfaceKey } from "@/pages/workspace/chat/useSurfaceKey";

function Probe() {
  const location = useLocation();
  return <div data-testid="loc">{location.pathname}</div>;
}

/** The portal's mount, keyed the way ChatPage keys it. */
function Latched() {
  const { chatId } = useParams<{ chatId: string }>();
  return <ChatSurface key={useSurfaceKey(chatId)} chatId={chatId} />;
}

/** The mount that remounts on every id — what the portal did before, and a
 *  shell that keys by id still could. */
function Remounting() {
  const { chatId } = useParams<{ chatId: string }>();
  return <ChatSurface key={chatId ?? "new"} chatId={chatId} />;
}

afterEach(() => {
  cleanup();
  state.chats = [];
  state.turns = [];
  state.createRejection = null;
  state.listeners = [];
  state.releaseCreate = () => {};
  vi.clearAllMocks();
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
});

function renderHome(Mount: () => ReactNode, { strict = false } = {}) {
  const qc = createQueryClient({ retry: false });
  const tree = (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat" element={<Mount />} />
          <Route path="/chat/:chatId" element={<Mount />} />
        </Routes>
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}

const field = (): HTMLElement => screen.getByRole("textbox", { name: /^message /i });
/** The transcript tape — the bubble is looked for there and nowhere else. */
const tape = (): HTMLElement => {
  const node = document.querySelector(".chat-tape");
  if (!(node instanceof HTMLElement)) throw new Error("no transcript tape on screen");
  return node;
};

async function send(text: string): Promise<void> {
  await act(async () => {
    fireEvent.change(field(), { target: { value: text } });
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
  });
}

async function landOnNewChat(): Promise<void> {
  await act(async () => {
    state.releaseCreate();
    await Promise.resolve();
  });
  await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/chat/c1"));
}

describe("starting a chat from the chats screen", () => {
  it("shows the transcript, the bubble and a usable composer before any request has answered", async () => {
    renderHome(Latched);
    await waitFor(() => expect(field()).toBeInTheDocument());

    await send(MESSAGE);

    // The create is parked; nothing has come back from anywhere.
    expect(ds.createChat).toHaveBeenCalledTimes(1);
    expect(ds.getChatTurns).not.toHaveBeenCalled();
    expect(screen.getByTestId("loc")).toHaveTextContent("/chat");
    expect(within(tape()).getByText(MESSAGE)).toBeInTheDocument();
    expect(screen.getByText(/^working/i)).toBeInTheDocument();
    expect(field()).toBeEnabled();
    expect(field()).toHaveValue("");
    // Nothing that reads as an opening chat, either.
    expect(screen.queryByRole("status", { name: /loading chat/i })).not.toBeInTheDocument();
  });

  it("keeps the same surface across the hop: the bubble and the composer are the same nodes", async () => {
    renderHome(Latched);
    await waitFor(() => expect(field()).toBeInTheDocument());
    await send(MESSAGE);
    const bubble = within(tape()).getByText(MESSAGE);
    const composer = field();

    await landOnNewChat();

    // Same DOM nodes on the created chat's route: no remount happened.
    expect(within(tape()).getByText(MESSAGE)).toBe(bubble);
    expect(field()).toBe(composer);
    expect(screen.getByText(/^working/i)).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: /loading chat/i })).not.toBeInTheDocument();
    expect(field()).toBeEnabled();
    expect(field()).toHaveValue("");
  });

  it("latches the key across the hop only; every other change of chat remounts", () => {
    const { result, rerender } = renderHook(({ id }: { id: string | undefined }) => useSurfaceKey(id), {
      initialProps: { id: undefined as string | undefined },
    });
    const fromComposer = result.current;

    // The hop onto a chat this tab is handing its first message to.
    useChatStore.getState().holdPendingTurns("c1", [optimisticUserTurn(MESSAGE)]);
    rerender({ id: "c1" });
    expect(result.current).toBe(fromComposer);

    // A switch to another existing chat: its own key.
    rerender({ id: "c2" });
    expect(result.current).toBe("c2");

    // Back to the composer: a fresh surface, not the created chat's.
    rerender({ id: undefined });
    const secondVisit = result.current;
    expect(secondVisit).not.toBe(fromComposer);

    // From the composer onto a chat WITHOUT a hand-off (a rail row): remounts.
    rerender({ id: "c3" });
    expect(result.current).toBe("c3");
  });

  it("survives development's second open of the same chat (StrictMode)", async () => {
    // A fresh surface per id, under StrictMode, which runs
    // the surface's open effect twice. The hand-off has to outlive the first.
    renderHome(Remounting, { strict: true });
    await waitFor(() => expect(field()).toBeInTheDocument());
    await send(MESSAGE);

    await landOnNewChat();

    expect(state.turns).toEqual([]);
    await waitFor(() => expect(within(tape()).getByText(MESSAGE)).toBeInTheDocument());
    expect(screen.getByText(/^working/i)).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: /loading chat/i })).not.toBeInTheDocument();
  });

  it("retires the hand-off with the echo — exactly one bubble, and a later open adopts nothing", async () => {
    renderHome(Latched, { strict: true });
    await waitFor(() => expect(field()).toBeInTheDocument());
    await send(MESSAGE);
    await landOnNewChat();
    expect(within(tape()).getAllByText(MESSAGE)).toHaveLength(1);

    state.turns = [ECHO];
    await act(async () => {
      for (const listener of state.listeners) listener({ replay: false });
      await Promise.resolve();
    });
    await waitFor(() => expect(useChatStore.getState().pending.c1).toBeUndefined());
    await waitFor(() => expect(within(tape()).getAllByText(MESSAGE)).toHaveLength(1));
    expect(useChatStore.getState().byId.c1?.optimistic).toEqual([]);
  });
});

describe("the hand-off slot", () => {
  it("is adopted by every open until the echo, then by none", async () => {
    const turn = optimisticUserTurn(MESSAGE);
    const store = useChatStore.getState();
    store.holdPendingTurns("c1", [turn]);

    // Two opens back to back — what an effect's double-invoke does.
    const first = store.open("c1");
    first();
    const second = useChatStore.getState().open("c1");
    await act(async () => {
      await Promise.resolve();
    });
    expect(useChatStore.getState().byId.c1.optimistic).toEqual([turn]);
    expect(useChatStore.getState().byId.c1.sending).toBe(true);
    expect(useChatStore.getState().pending.c1).toEqual([turn]);

    // The echo lands: the placeholder and the slot go together.
    state.turns = [ECHO];
    await act(async () => {
      for (const listener of state.listeners) listener({ replay: false });
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() => expect(useChatStore.getState().pending.c1).toBeUndefined());
    expect(useChatStore.getState().byId.c1.optimistic).toEqual([]);
    second();

    // A re-open after the echo gets the folded transcript and nothing else.
    const third = useChatStore.getState().open("c1");
    expect(useChatStore.getState().byId.c1.optimistic).toEqual([]);
    third();
  });
});

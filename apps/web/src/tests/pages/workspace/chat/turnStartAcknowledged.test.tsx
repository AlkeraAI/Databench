// "Working…" is the machine's word, not the tab's.
//
// The case this pins: a new chat's first message, the workspace up and idle,
// and the box slow to take it. The tab must not invent "Working…", a blank
// reload, or "Queued behind the running turn" with no turn running. The record
// decides: until the machine
// has started the turn — its document says working, or the tape shows
// something it wrote after the message — the message is waiting for the
// workspace, in the tab that sent it and after a reload alike.

import type { ConversationTurn } from "@alkera/chat-model";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";

const WORKING = "Working…";
const QUEUED_BEHIND = "Queued behind the running turn";
const AT = "2026-09-24T22:05:30Z";

/** The person's first message, on the record, as the fold reads a prompt no
 *  box has echoed yet: still going out. */
const ASKED: ConversationTurn = {
  id: "usr:first",
  author: "user",
  status: "running",
  parts: [{ id: "u1-t", kind: "text", text: "select count(*) from orders" }],
};
/** The same message once the box has echoed it — handed to the agent. */
const TAKEN: ConversationTurn = { ...ASKED, status: "done" };
/** The first thing the agent writes. */
const STARTED: ConversationTurn = {
  id: "a1",
  author: "assistant",
  status: "running",
  startedAt: AT,
  parts: [],
};

const { ds, state } = vi.hoisted(() => {
  const state = {
    turns: [] as unknown[],
    turnState: "idle" as string,
    listeners: new Set<(event: { replay: boolean }) => void>(),
    posted: [] as string[],
  };
  const ds = {
    // What the cloud source says of itself: a message is recorded and a box
    // starts its turn later, and a mid-turn send waits behind the running one.
    holdsSendsBehindTurn: true,
    startsTurnsRemotely: true,
    listChats: async () => [{ id: "c1", title: "Chat", updatedAt: "2026-09-24T22:05:00Z" }],
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
        body: JSON.stringify({ text: content }),
      });
      state.posted.push(content);
      if (!response.ok) throw new ApiError(response.status, await response.json());
      return { id: `m-${state.posted.length}`, role: "user", content };
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
      if (method === "POST" && raw.includes("/messages")) return answer(201, { id: "m1" });
      return answer(200, {});
    }),
  );
}

/** The chat as the browser mounts it with the workspace READY: the banner has
 *  nothing to say and the machine is not waited on. */
function renderChat() {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:id" element={<ChatSurface chatId="c1" machineWaiting={false} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** A reload: the tab's memory goes, the record stays. */
function reload(): ReturnType<typeof renderChat> {
  cleanup();
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
  return renderChat();
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

/** The box publishes: the record now holds `turns`, the document says
 *  `turnState`, and the open chat hears a live frame. */
async function boxPublishes(turns: ConversationTurn[], turnState: string): Promise<void> {
  state.turns = turns;
  state.turnState = turnState;
  await act(async () => {
    for (const listener of state.listeners) listener({ replay: false });
    await Promise.resolve();
  });
}

beforeEach(() => {
  state.turns = [];
  state.turnState = "idle";
  state.listeners.clear();
  state.posted = [];
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
  stubApi();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("a message the ready workspace has not taken up yet", () => {
  it("waits for the workspace, not working, and keeps the turn's composer", async () => {
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());

    await send("select count(*) from orders");
    // The server has recorded it; nothing from the box follows it.
    await boxPublishes([ASKED], "idle");

    expect(await screen.findByText(WAITING_FOR_MACHINE)).toBeInTheDocument();
    expect(screen.queryByText(WORKING)).toBeNull();
    // The message is in flight: the reader can stop it, and the composer does
    // not invite the same words again as if the chat were settled.
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("works once the machine's document says the turn started", async () => {
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
    await send("select count(*) from orders");
    await boxPublishes([ASKED], "idle");
    expect(await screen.findByText(WAITING_FOR_MACHINE)).toBeInTheDocument();

    await boxPublishes([ASKED], "working");

    expect(await screen.findByText(WORKING)).toBeInTheDocument();
    expect(screen.queryByText(WAITING_FOR_MACHINE)).toBeNull();
  });

  it("works once the tape shows the box writing after the message", async () => {
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
    await send("select count(*) from orders");
    await boxPublishes([ASKED], "idle");
    expect(await screen.findByText(WAITING_FOR_MACHINE)).toBeInTheDocument();

    await boxPublishes([TAKEN, STARTED], "idle");

    expect(await screen.findByText(WORKING)).toBeInTheDocument();
    expect(screen.queryByText(WAITING_FOR_MACHINE)).toBeNull();
  });

  it("works once the box has echoed the message, before the agent says anything", async () => {
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
    await send("select count(*) from orders");
    await boxPublishes([ASKED], "idle");
    expect(await screen.findByText(WAITING_FOR_MACHINE)).toBeInTheDocument();

    await boxPublishes([TAKEN], "idle");

    expect(await screen.findByText(WORKING)).toBeInTheDocument();
    expect(screen.queryByText(WAITING_FOR_MACHINE)).toBeNull();
  });

  it("reads the same after a reload, from the record alone", async () => {
    state.turns = [ASKED];
    renderChat();

    expect(await screen.findByText(WAITING_FOR_MACHINE)).toBeInTheDocument();
    expect(screen.queryByText(WORKING)).toBeNull();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();

    // And a reload after the turn started says working once the running turn
    // is heard — a turn the tab did not start is held open by the machine's
    // live word, never by a replay alone.
    state.turns = [TAKEN, STARTED];
    reload();
    await boxPublishes([TAKEN, STARTED], "working");
    expect(await screen.findByText(WORKING)).toBeInTheDocument();
    expect(screen.queryByText(WAITING_FOR_MACHINE)).toBeNull();
  });

  it("an answered chat reloads settled", async () => {
    state.turns = [TAKEN, { ...STARTED, status: "done", completedAt: AT }];
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
    expect(screen.queryByText(WAITING_FOR_MACHINE)).toBeNull();
    expect(screen.queryByText(WORKING)).toBeNull();
  });
});

describe("a second message while the first has not been taken up", () => {
  it("is not said to be queued behind a running turn", async () => {
    state.turns = [ASKED];
    renderChat();
    expect(await screen.findByText(WAITING_FOR_MACHINE)).toBeInTheDocument();

    await act(async () => {
      useChatStore.getState().send("c1", "are you there?");
    });
    await waitFor(() => expect(state.posted).toEqual(["are you there?"]));
    await boxPublishes([ASKED, { ...ASKED, id: "usr:second" }], "idle");

    expect(useChatStore.getState().byId.c1?.queuedBehind ?? null).toBeNull();
    expect(screen.queryByText(QUEUED_BEHIND)).toBeNull();
    expect(screen.getByText(WAITING_FOR_MACHINE)).toBeInTheDocument();
  });

  it("is queued behind a turn the record shows running", async () => {
    renderChat();
    await waitFor(() => expect(state.listeners.size).toBeGreaterThan(0));
    await boxPublishes([TAKEN, STARTED], "working");
    expect(await screen.findByText(WORKING)).toBeInTheDocument();

    await act(async () => {
      useChatStore.getState().send("c1", "and by month?");
    });

    expect(await screen.findByText(QUEUED_BEHIND)).toBeInTheDocument();
  });
});

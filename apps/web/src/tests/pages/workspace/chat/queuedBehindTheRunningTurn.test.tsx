// A message sent into a chat that is already mid-turn, and the line that says so.
//
// A chat opened while a turn is running does not read as busy — the transcript's
// word on a turn this tab did not start is not enough to take the composer away
// — so the reader types and presses Send. The server takes the message (201,
// recorded), and the box, which runs one turn at a time, parks it until the turn
// in front of it ends. Nothing about that was visible: the field cleared, the
// transcript did not change, and for as long as the running turn lasted the
// reader had no way to tell an accepted message from a lost one.
//
// What is pinned here is the whole span of the wait: the line appears the moment
// the server takes the message, and it goes when the turn it was waiting behind
// ends — on the transcript saying so, not on a clock.

import type { ConversationTurn } from "@alkera/chat-model";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";

/** The line under the field. Spelled out rather than imported: it is the whole
 *  of what the reader gets, so a silent rewording is a regression. */
const QUEUED_LINE = "Queued behind the running turn";

const AT = "2026-09-06T12:00:00Z";

/** The turn already in flight when the reader arrives: a question answered, and
 *  an assistant turn nobody has closed. */
const ASKED: ConversationTurn = {
  id: "u1",
  author: "user",
  status: "done",
  parts: [{ id: "u1-t", kind: "text", text: "count the orders" }],
};
const ANSWERING: ConversationTurn = {
  id: "a1",
  author: "assistant",
  status: "running",
  startedAt: AT,
  parts: [{ id: "a1-t", kind: "text", text: "reading the warehouse" }],
};
/** The same turn, finished — which is what releases the waiting message. */
const ANSWERED: ConversationTurn = { ...ANSWERING, status: "done", completedAt: AT };

const { ds, state } = vi.hoisted(() => {
  const state = {
    turns: [] as unknown[],
    /** The open chat's live listener, so a test can push a frame at it. */
    listeners: new Set<(event: unknown) => void>(),
    /** Every POST the source made, with the status the server answered. */
    posted: [] as { text: string; status: number }[],
  };
  const ds = {
    // The cloud holds a message it accepts mid-turn behind the running turn
    // rather than superseding it. Without this the source makes no such claim
    // and the composer says nothing.
    holdsSendsBehindTurn: true,
    listChats: async () => [{ id: "c1", title: "Chat", updatedAt: AT }],
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: async () => state.turns,
    searchFiles: async () => [] as unknown[],
    // The send goes over the wire, so the 201 the route answers is a real
    // response parsed by the real error type — not a resolved promise shaped
    // to look like one.
    sendUserMessage: async (chatId: string, content: string) => {
      const response = await fetch(`/api/v1/chats/${chatId}/messages`, {
        method: "POST",
        body: JSON.stringify({ text: content }),
      });
      state.posted.push({ text: content, status: response.status });
      if (!response.ok) throw new ApiError(response.status, await response.json());
      return { id: `m-${state.posted.length}`, role: "user", content };
    },
    getPermissionMode: async () => "default",
    setPermissionMode: async () => {},
    subscribePermissionMode: () => () => {},
    subscribeChat: (_chatId: string, onEvent: (event: unknown) => void) => {
      state.listeners.add(onEvent);
      return () => void state.listeners.delete(onEvent);
    },
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
      // What the route really answers: the message is CREATED, whether a turn
      // is running or not. Nothing in the response says it will have to wait.
      if (method === "POST" && raw.includes("/messages")) return answer(201, { id: "m1" });
      return answer(200, {});
    }),
  );
}

function renderChat() {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:id" element={<ChatSurface chatId="c1" />} />
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

/** The machine published something: the store re-reads the turns and
 *  reconciles them as LIVE, exactly as an open chat's socket frame does. */
async function machineSpoke(): Promise<void> {
  await act(async () => {
    state.listeners.forEach((listener) =>
      listener({ id: "ev-1", chatId: "c1", kind: "assistant_text", content: "" }),
    );
    await Promise.resolve();
  });
}

beforeEach(() => {
  state.turns = [];
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

describe("a message sent while a turn is already running", () => {
  it("says it is waiting, from the moment the server takes it", async () => {
    state.turns = [ASKED, ANSWERING];
    renderChat();
    // The chat opened mid-turn, so the composer is the reader's: this is the
    // send that reaches the server instead of being held in the browser.
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());

    await send("and group them by region");

    expect(state.posted).toEqual([{ text: "and group them by region", status: 201 }]);
    expect(await screen.findByText(QUEUED_LINE)).toBeInTheDocument();
  });

  it("stops saying it once the turn it was waiting behind ends", async () => {
    state.turns = [ASKED, ANSWERING];
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
    await send("and group them by region");
    expect(await screen.findByText(QUEUED_LINE)).toBeInTheDocument();

    // The turn in front finishes and the box takes the waiting message: the
    // reader's words are echoed and an answer to THEM opens.
    state.turns = [
      ASKED,
      ANSWERED,
      {
        id: "u2",
        author: "user",
        status: "done",
        parts: [{ id: "u2-t", kind: "text", text: "and group them by region" }],
      },
      {
        id: "a2",
        author: "assistant",
        status: "running",
        startedAt: AT,
        parts: [{ id: "a2-t", kind: "text", text: "grouping" }],
      },
    ];
    await machineSpoke();

    await waitFor(() => expect(screen.queryByText(QUEUED_LINE)).toBeNull());
    // And the ordinary running-turn composer has taken over: the key is Stop,
    // because the turn now running is the reader's own.
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("goes on saying it while the turn in front is still running", async () => {
    // The fact is the turn in front, not the passage of time or the arrival of
    // frames: a turn that publishes for minutes must not retire the line.
    state.turns = [ASKED, ANSWERING];
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());
    await send("and group them by region");
    expect(await screen.findByText(QUEUED_LINE)).toBeInTheDocument();

    state.turns = [
      ASKED,
      { ...ANSWERING, parts: [...ANSWERING.parts, { id: "a1-t2", kind: "text", text: "still going" }] },
      {
        id: "u2",
        author: "user",
        status: "done",
        parts: [{ id: "u2-t", kind: "text", text: "and group them by region" }],
      },
    ];
    await machineSpoke();

    expect(screen.getByText(QUEUED_LINE)).toBeInTheDocument();
  });
});

describe("a message sent into a chat that owes nothing", () => {
  it("says nothing about a queue — it was not queued", async () => {
    state.turns = [];
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());

    await send("count the orders");

    expect(state.posted).toEqual([{ text: "count the orders", status: 201 }]);
    // The same 201 as the queued send: the status alone never said which was
    // which, and the line must not appear on the one that starts at once.
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument());
    expect(screen.queryByText(QUEUED_LINE)).toBeNull();
  });

  it("says nothing on a source that supersedes a mid-turn send", async () => {
    // The editor's daemon hands a prompt over a live turn straight to the
    // harness, which withdraws the turn in flight. Nothing is queued there, so
    // nothing may claim it is.
    ds.holdsSendsBehindTurn = false;
    try {
      state.turns = [ASKED, ANSWERING];
      renderChat();
      await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());

      await send("and group them by region");

      expect(state.posted).toHaveLength(1);
      expect(screen.queryByText(QUEUED_LINE)).toBeNull();
    } finally {
      ds.holdsSendsBehindTurn = true;
    }
  });
});

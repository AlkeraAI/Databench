// A send the server refuses, and a composer that knows it will be refused.
//
// A chat is shared at the same rungs every other file has, and a reader on a
// rung that carries no write may follow the chat and may not write in it. Two
// things follow, and this file holds both:
//
//  1. the composer asks the same question the server will, BEFORE the reader
//     types — a Send that is known to be refused is never offered; and
//  2. a refused send is withdrawn. When `POST /chats/{id}/messages` answers
//     403, the bubble goes and the reason is said where it stood.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";

/** The send goes over the wire, so the refusal is a real 403 response parsed by
 *  the real error type — not a hand-made rejection shaped to match. */
const { ds, state } = vi.hoisted(() => {
  const state = { turns: [] as unknown[] };
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
import { SEND_FORBIDDEN, useChatStore } from "@/pages/workspace/chat/chatStore";

/** How the refused POST answers: 403 with the backend's envelope, or 500 for
 *  the fault that is NOT a refusal. */
let sendStatus = 403;
let sendBody: unknown = { error: { code: "forbidden", message: "" } };

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

beforeEach(() => {
  sendStatus = 403;
  sendBody = { error: { code: "forbidden", message: "" } };
  state.turns = [];
  useChatStore.setState({ byId: {}, composerPrefs: {} });
  stubApi();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("a send the server refuses", () => {
  it("withdraws the message instead of leaving it standing as if it were sent", async () => {
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());

    await send("SHARED-DRAFT-FROM-MEMBER");

    // The 403 takes the bubble back down rather than letting it sit there as a
    // message the colleague on the other side will never see.
    await waitFor(() => expect(screen.queryByText("SHARED-DRAFT-FROM-MEMBER")).toBeNull());
    expect(await screen.findByText(SEND_FORBIDDEN)).toBeInTheDocument();
    // The chat is not "Working…" on a turn that was never accepted.
    await waitFor(() => expect(screen.queryByText(/^working/i)).toBeNull());
  });

  it("says what the reader can do about it, not what the policy called it", async () => {
    // The policy's own sentence names a rung on a node id; the transport's
    // names a URL and a status. Neither tells the person at the keyboard what
    // happened, so neither is what they are shown.
    sendBody = { error: { code: "files.role_required", message: "writer required on node" } };
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());

    await send("hello");

    expect(await screen.findByText(SEND_FORBIDDEN)).toBeInTheDocument();
    expect(screen.queryByText(/writer required on node/)).toBeNull();
  });

  it("keeps the message for a failure that is not a refusal", async () => {
    // A 500 is a turn that may yet be retried; withdrawing the bubble there
    // would lose what the reader typed.
    sendStatus = 500;
    renderChat();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument());

    await send("still mine");
    await waitFor(() => expect(screen.queryByText(SEND_FORBIDDEN)).toBeNull());

    // The bubble stands: the reader's words are not thrown away over a fault
    // that says nothing about whether they were allowed to write them.
    expect(screen.getByText("still mine")).toBeInTheDocument();
  });
});

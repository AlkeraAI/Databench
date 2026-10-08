// The Stop that has gone and not come back yet.
//
// Stop is a relay: the press posts to the server, the server puts it on the
// chat's channel, the box ends the turn. That round trip is long enough to read
// as nothing having happened — and the key gave the reader nothing to read. It
// flipped straight back to Send (the composer is released the moment Stop is
// pressed), so an owner who had just stopped a turn saw a composer that looked
// exactly like one they had never pressed anything in. They pressed again.
// Three stops in nine seconds, three relays, one turn.
//
// So the press has a receipt: for as long as the stop is outstanding the key
// states it and takes no further press. Both ends of the round trip free it —
// a stop that FAILED must hand the key back too, or the turn is unstoppable.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

/** The stop goes over the wire through the REAL source, so what is counted here
 *  is requests that actually left the browser — not presses a spy recorded. */
const { ds } = vi.hoisted(() => ({
  ds: {
    listChats: async () => [{ id: "c1", title: "Daily citations", updatedAt: "2026-09-21T12:00:00Z" }],
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: async () => [] as unknown[],
    searchFiles: async () => [] as unknown[],
    sendUserMessage: async (_chatId: string, content: string) => ({
      id: "m1",
      role: "user",
      content,
    }),
    cancelTurn: (chatId: string) => new CloudDataSource().cancelTurn(chatId),
    getPermissionMode: async () => "default",
    setPermissionMode: async () => {},
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
  },
}));

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const host = real.createBrowserChatHost({
    account: () => ({ email: "owner@tideline.example", webAppUrl: null }),
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

/** Every stop the browser actually posted, and the answer each one is still
 *  waiting for. Holding them open is the whole point: the window this file is
 *  about is the one where the reader has pressed and nothing has come back. */
const posted: string[] = [];
let answer: { ok: (response: Response) => void; fail: (err: unknown) => void } | null = null;

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input instanceof Request ? input.url : input);
      if (url.includes("/stop")) {
        posted.push(url);
        return new Promise<Response>((resolve, reject) => {
          answer = { ok: resolve, fail: reject };
        });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

function renderChat(): void {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:id" element={<ChatSurface chatId="c1" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Ask something, so there is a turn of this reader's own to stop. */
async function askSomething(text: string): Promise<void> {
  const box = await screen.findByRole("textbox", { name: /^message /i });
  await act(async () => {
    fireEvent.change(box, { target: { value: text } });
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
  });
}

/** The one key at the end of the rail, whatever it currently says. */
function sendKey(): HTMLButtonElement {
  const key = document.querySelector<HTMLButtonElement>(".chat-composer-send");
  if (!key) throw new Error("the composer has no send key");
  return key;
}

function type(text: string): void {
  fireEvent.change(screen.getByRole("textbox", { name: /^message /i }), {
    target: { value: text },
  });
}

beforeEach(() => {
  posted.length = 0;
  answer = null;
  useChatStore.setState({ byId: {}, composerPrefs: {} });
  stubApi();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("Stop, while the stop is still on the wire", () => {
  it("takes one stop from a double press, and says the first one went", async () => {
    renderChat();
    await askSomething("summarise the citations");
    const key = await screen.findByRole("button", { name: "Stop" });

    // Two presses of the same key, the way an impatient double-click lands:
    // both before anything on screen has had a chance to change.
    await act(async () => {
      fireEvent.click(key);
      fireEvent.click(key);
    });

    expect(posted).toEqual([expect.stringContaining("/api/v1/chats/c1/stop")]);
    expect(sendKey()).toHaveAccessibleName("Stopping…");
    expect(sendKey()).toBeDisabled();

    // A third press, now that the key visibly says what it is waiting for.
    await act(async () => {
      fireEvent.click(sendKey());
    });
    expect(posted).toHaveLength(1);
  });

  it("gives the key back when the stop lands", async () => {
    renderChat();
    await askSomething("summarise the citations");
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    });
    expect(sendKey()).toHaveAccessibleName("Stopping…");

    // Words typed while the stop is outstanding do not unlock the key: the
    // press is still unanswered, and the key is the receipt for it.
    await act(async () => {
      type("and the second question");
    });
    expect(sendKey()).toBeDisabled();
    expect(sendKey()).toHaveAccessibleName("Stopping…");

    await act(async () => {
      answer?.ok(new Response(null, { status: 202 }));
    });

    // The key is the reader's again — a live Send over what they typed, not a
    // key still dimmed on a stop that has already happened.
    await waitFor(() => expect(sendKey()).toHaveAccessibleName("Send"));
    expect(sendKey()).toBeEnabled();
  });

  it("gives the key back when the stop fails, and sends the next one", async () => {
    renderChat();
    await askSomething("summarise the citations");
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    });
    expect(sendKey()).toHaveAccessibleName("Stopping…");

    // Nothing answered the post, which is the one case where the turn really
    // may still be running — so the reader is told, and above all is left able
    // to press again.
    await act(async () => {
      answer?.fail(new TypeError("Failed to fetch"));
    });

    expect(
      await screen.findByText("The stop did not reach the machine. The turn is still running."),
    ).toBeInTheDocument();
    await waitFor(() => expect(sendKey()).not.toHaveAccessibleName("Stopping…"));

    // Released, not merely repainted: the next turn's Stop reaches the server.
    await askSomething("try that again");
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    });
    expect(posted).toHaveLength(2);
  });
});

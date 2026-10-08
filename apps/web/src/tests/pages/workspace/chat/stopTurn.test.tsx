// Pressing Stop.
//
// Stopping is the data source's verb, not the shell's: the editor cancels on
// the daemon's engine channel, the portal has to reach the machine running the
// turn. Whatever the source manages,
// the composer is released and the outcome is on screen.

import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SEND_FORBIDDEN } from "@/pages/workspace/chat/chatStore";
import type { CancelOutcome } from "@/pages/workspace/chat/data/ChatDataSource";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import { createBrowserChatHost } from "@/pages/workspace/chat/data/browserChatHost";

const { ds, engineRequest, subscribers } = vi.hoisted(() => {
  const engineRequest = vi.fn(async () => ({}));
  const subscribers = new Set<(e: { replay: boolean }) => void>();
  return {
    engineRequest,
    subscribers,
    ds: {
      cancelTurn: vi.fn(
        async (_chatId: string): Promise<CancelOutcome> => ({
          stopped: false,
          reason: "the workspace is still finishing this turn",
        }),
      ),
      listChats: vi.fn(async () => [{ id: "c1", title: "Daily citations", permissionMode: "default" }]),
      listModels: async () => [],
      resolveChatDefaults: async () => ({ model: null, effort: null }),
      listCommands: async () => [],
      getChatTurns: vi.fn(async () => [] as unknown[]),
      getChatMessages: async () => [],
      searchFiles: async () => [],
      subscribeChat: vi.fn((_chatId: string, cb: (e: { replay: boolean }) => void) => {
        subscribers.add(cb);
        return () => subscribers.delete(cb);
      }),
      subscribePermissionMode: () => () => {},
      getPermissionMode: async () => "default",
      setPermissionMode: async () => {},
      setEffort: async () => {},
      createChat: vi.fn(),
      sendUserMessage: vi.fn(async () => {}),
      fetchBlob: vi.fn(),
    },
  };
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  return {
    ...actual,
    chatData: () => ds,
    chatCaps: () => ({ opencodeActive: true }),
    chatHost: () => ({
      kind: "browser" as string,
      engine: { request: engineRequest },
      auth: { request: vi.fn(async () => ({})) },
      subscribe: () => () => {},
      runCommand: vi.fn(async () => {}),
      openFile: vi.fn(async () => {}),
      openPlanDocument: vi.fn(async () => {}),
      workspacePath: () => null,
      account: () => ({ email: null, webAppUrl: null }),
      onAccountChange: () => () => {},
    }),
  };
});

import { useChatController } from "@/pages/workspace/chat/controller";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<>{children}</>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  subscribers.clear();
  ds.getChatTurns.mockResolvedValue([]);
});

/** The turn the machine is still writing: no `completedAt`, so the composer
 *  derives "a response is owed" from it. */
const runningTurn = [
  { id: "u1", author: "user", parts: [{ id: "u1-t", kind: "text", text: "ask" }] },
  { id: "a1", author: "assistant", status: "running", parts: [], completedAt: null },
];

describe("Stop in the portal", () => {
  it("asks the data source, never the host's engine channel", async () => {
    const { result } = renderHook(() => useChatController(), { wrapper });
    await waitFor(() => expect(result.current.identity.chatId).toBe("c1"));

    await act(async () => {
      result.current.transcript.cancelTurn();
    });

    expect(ds.cancelTurn).toHaveBeenCalledExactlyOnceWith("c1");
    expect(engineRequest).not.toHaveBeenCalled();
  });

  it("shows what Stop could not do, and clears it on dismiss", async () => {
    const { result } = renderHook(() => useChatController(), { wrapper });
    await waitFor(() => expect(result.current.identity.chatId).toBe("c1"));

    await act(async () => {
      result.current.transcript.cancelTurn();
    });

    await waitFor(() =>
      expect(result.current.transcript.cancelNotice).toBe(
        "the workspace is still finishing this turn",
      ),
    );

    act(() => result.current.transcript.dismissCancelNotice());
    expect(result.current.transcript.cancelNotice).toBeNull();
  });

  it("says nothing when the source really stopped the turn", async () => {
    ds.cancelTurn.mockResolvedValueOnce({ stopped: true, reason: undefined as unknown as string });
    const { result } = renderHook(() => useChatController(), { wrapper });
    await waitFor(() => expect(result.current.identity.chatId).toBe("c1"));

    await act(async () => {
      result.current.transcript.cancelTurn();
    });

    await waitFor(() => expect(ds.cancelTurn).toHaveBeenCalled());
    expect(result.current.transcript.cancelNotice).toBeNull();
  });

  it("surfaces a rejected cancel instead of leaving it unhandled", async () => {
    ds.cancelTurn.mockRejectedValueOnce(new Error("relay refused"));
    const { result } = renderHook(() => useChatController(), { wrapper });
    await waitFor(() => expect(result.current.identity.chatId).toBe("c1"));

    await act(async () => {
      result.current.transcript.cancelTurn();
    });

    await waitFor(() => expect(result.current.transcript.cancelNotice).toContain("relay refused"));
  });

  it("keeps the composer free while the stopped turn goes on streaming", async () => {
    // Stop cannot abort the machine here — the notice says so. What it CAN do is
    // give the composer back, and the very next token must not take it away
    // again: the presenter reads "Stopped waiting", starts typing the next
    // question, and the send key flips back to Stop under their cursor.
    ds.getChatTurns.mockResolvedValue(runningTurn);
    const { result } = renderHook(() => useChatController(), { wrapper });
    await waitFor(() => expect(result.current.identity.chatId).toBe("c1"));

    await act(async () => {
      result.current.transcript.cancelTurn();
    });
    await waitFor(() => expect(result.current.transcript.sending).toBe(false));

    // A chunk of the answer the machine is still writing.
    await act(async () => {
      subscribers.forEach((cb) => cb({ replay: false }));
    });

    expect(result.current.transcript.sending).toBe(false);
  });

  it("takes the composer back when the reader sends again", async () => {
    // The release is for the turn that was stopped, not forever: a new send
    // owns the composer the way any send does.
    ds.getChatTurns.mockResolvedValue(runningTurn);
    const { result } = renderHook(() => useChatController(), { wrapper });
    await waitFor(() => expect(result.current.identity.chatId).toBe("c1"));

    await act(async () => {
      result.current.transcript.cancelTurn();
    });
    await waitFor(() => expect(result.current.transcript.sending).toBe(false));

    await act(async () => {
      result.current.transcript.submitMessage("the second question");
    });

    expect(result.current.transcript.sending).toBe(true);
  });
});

describe("a Stop the workspace refused", () => {
  // The real source's `cancelTurn` under the real controller, so what the reader
  // reads is decided by the two together. A refused Stop DID reach the workspace —
  // telling the reader it did not is the one thing the notice must never do.
  const answerWith = (status: number, body: unknown) => {
    ds.cancelTurn.mockImplementation((chatId: string) => new CloudDataSource().cancelTurn(chatId));
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        }),
    );
  };

  afterEach(() => vi.unstubAllGlobals());

  it("names the rung when a reader who may not send presses Stop", async () => {
    answerWith(403, { error: { code: "forbidden", message: "Not permitted on this resource." } });
    const { result } = renderHook(() => useChatController(), { wrapper });
    await waitFor(() => expect(result.current.identity.chatId).toBe("c1"));

    await act(async () => {
      result.current.transcript.cancelTurn();
    });

    await waitFor(() => expect(result.current.transcript.cancelNotice).toBe(SEND_FORBIDDEN));
  });

  it("shows the server's own sentence when the workspace failed the stop", async () => {
    answerWith(500, {
      error: { code: "internal_error", message: "The chat's box is unreachable." },
    });
    const { result } = renderHook(() => useChatController(), { wrapper });
    await waitFor(() => expect(result.current.identity.chatId).toBe("c1"));

    await act(async () => {
      result.current.transcript.cancelTurn();
    });

    await waitFor(() =>
      expect(result.current.transcript.cancelNotice).toBe("The chat's box is unreachable."),
    );
  });

  it("keeps the transport sentence when the stop never left the browser", async () => {
    // Nothing answered, so the turn really may still be running — the only case
    // the "did not reach the workspace" sentence is true of.
    ds.cancelTurn.mockImplementation((chatId: string) => new CloudDataSource().cancelTurn(chatId));
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });
    const { result } = renderHook(() => useChatController(), { wrapper });
    await waitFor(() => expect(result.current.identity.chatId).toBe("c1"));

    await act(async () => {
      result.current.transcript.cancelTurn();
    });

    await waitFor(() =>
      expect(result.current.transcript.cancelNotice).toBe(
        "The stop did not reach the machine. The turn is still running.",
      ),
    );
  });
});

describe("the browser source's own Stop", () => {
  it("goes out on the chat's REST surface, never the engine channel", async () => {
    // The real source, over the real browser host — the host whose engine
    // channel rejects by design. Nothing here may reach that channel.
    const host = createBrowserChatHost({ account: () => ({ email: null, webAppUrl: null }) });
    await expect(host.engine.request("chat.cancel", { chatId: "c1" })).rejects.toThrow(
      /no engine channel/,
    );

    const calls: string[] = [];
    vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return new Response(null, { status: 202 });
    });
    try {
      const outcome = await new CloudDataSource().cancelTurn("c1");
      expect(calls).toEqual([expect.stringContaining("/api/v1/chats/c1/stop")]);
      expect(outcome.stopped).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

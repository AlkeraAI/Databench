// Which chat a chat route is on.
//
// `/chat` is the NEW-chat surface in both shells: nothing open, nothing in the
// transcript, and a crumb that says so. It never adopts whichever chat the list
// returned first. The route's own parameter — `:chatId` in the portal, `:id` in the
// editor — is the only thing that opens a chat.

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { ds } = vi.hoisted(() => {
  const turns = [
    {
      id: "t1",
      role: "user" as const,
      parts: [{ id: "p1", kind: "text" as const, text: "how many citations yesterday?" }],
    },
  ];
  return {
    ds: {
      listChats: vi.fn(async () => [
        { id: "c-newest", title: "Daily citations", permissionMode: "default" },
        { id: "c-older", title: "Order volume", permissionMode: "default" },
      ]),
      listModels: async () => [],
      resolveChatDefaults: async () => ({ model: null, effort: null }),
      listCommands: async () => [],
      getChatTurns: vi.fn(async () => turns),
      getChatMessages: async () => [],
      searchFiles: async () => [],
      subscribeChat: () => () => {},
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

vi.mock("@/pages/workspace/chat/data", () => ({
  chatData: () => ds,
  chatCaps: () => ({ opencodeActive: true }),
  chatHost: () => ({
    kind: "browser" as string,
    engine: { request: vi.fn(async () => ({})) },
    auth: { request: vi.fn(async () => ({})) },
    subscribe: () => () => {},
    runCommand: vi.fn(async () => {}),
    openFile: vi.fn(async () => {}),
    openPlanDocument: vi.fn(async () => {}),
    workspacePath: () => null,
    account: () => ({ email: null, webAppUrl: null }),
    onAccountChange: () => () => {},
  }),
  errorText: (err: unknown) => String(err),
  exportBlob: async () => {},
  isSessionNotOpen: () => false,
  refetchWhileErrored: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
}));

import { useChatController } from "@/pages/workspace/chat/controller";

function wrapperAt(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/chat" element={<>{children}</>} />
            <Route path="/chat/:chatId" element={<>{children}</>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  };
}

/** The editor's own route name for the same surface. */
function editorWrapperAt(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/chat/:id" element={<>{children}</>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("the chat a chat route is on", () => {
  it("opens no chat at /chat, however many the org has", async () => {
    const { result } = renderHook(() => useChatController(), { wrapper: wrapperAt("/chat") });

    // Let the chat list land: the bug only showed once it had.
    await waitFor(() => expect(ds.listChats).toHaveBeenCalled());
    await waitFor(() => expect(result.current.composer.daemonCommands).toBeDefined());

    expect(result.current.identity.chatId).toBeNull();
    expect(result.current.identity.title).toBe("New chat");
    expect(result.current.transcript.renderedTurns).toEqual([]);
    expect(ds.getChatTurns).not.toHaveBeenCalled();
  });

  it("opens the chat the portal's :chatId names, under its own title", async () => {
    const { result } = renderHook(() => useChatController(), {
      wrapper: wrapperAt("/chat/c-older"),
    });

    await waitFor(() => expect(result.current.identity.title).toBe("Order volume"));
    expect(result.current.identity.chatId).toBe("c-older");
    expect(result.current.identity.currentTitle).toBe("Order volume");
    expect(result.current.identity.title).not.toBe("New chat");
    await waitFor(() => expect(result.current.transcript.renderedTurns.length).toBe(1));
  });

  it("opens the chat the editor's :id names", async () => {
    const { result } = renderHook(() => useChatController(), {
      wrapper: editorWrapperAt("/chat/c-older"),
    });

    await waitFor(() => expect(result.current.identity.chatId).toBe("c-older"));
    expect(result.current.identity.title).toBe("Order volume");
  });
});

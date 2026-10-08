// A reader who may only follow the chat.
//
// A disabled field is still an invitation: it looks focusable, it takes a
// click, and the reader goes on trying to accept it. A live Stop is worse, a
// key over a turn that is not theirs.
//
// So the server says whether THIS reader may send (`canSend` on the chat read)
// and the dock is what answers: a quiet line in place of the composer, with
// nothing beside it to press. The unknown is the case that matters most — a
// deployment whose chat read predates the field answers without it, and
// `undefined` has to read as "not said", never as "no", or an older server
// would lock every reader out of a chat that is theirs.

import { cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

/** The chat data source, stubbed at the boundary the surface reads through, so
 *  the dock is the real one and nothing costly stands behind it. */
const { ds } = vi.hoisted(() => {
  const ds = {
    listChats: async () => [{ id: "c1", title: "Chat", updatedAt: "2026-09-06T12:00:00Z" }],
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: async () => [] as unknown[],
    searchFiles: async () => [] as unknown[],
    sendUserMessage: async () => ({ id: "m1", role: "user", content: "" }),
    getPermissionMode: async () => "default",
    setPermissionMode: async () => {},
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
  };
  return { ds };
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const host = real.createBrowserChatHost({
    account: () => ({ email: "dana@example.com", webAppUrl: null }),
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

import { ChatSurface, READ_ONLY_DOCK_NOTE } from "@/pages/workspace/chat/ChatSurface";

function mount(canSend: boolean | undefined) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({})),
  );
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <ChatSurface chatId="c1" canSend={canSend} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("the dock a reader who may not send gets", () => {
  it("has no field to type in and no key to press", async () => {
    mount(false);

    expect(await screen.findByText(READ_ONLY_DOCK_NOTE)).toBeTruthy();
    // Not a disabled field — no field. And nothing in the dock to press:
    // neither Send nor the Stop it becomes while a turn runs.
    expect(screen.queryByRole("textbox", { name: /^message /i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Send/ })).toBeNull();
  });

  it("keeps the composer when the server has not said", async () => {
    // The whole reason the field is optional: a chat read from before it
    // existed must not be read as a refusal.
    mount(undefined);

    expect(await screen.findByRole("textbox", { name: /^message /i })).toBeTruthy();
    expect(screen.queryByText(READ_ONLY_DOCK_NOTE)).toBeNull();
  });

  it("keeps the composer for a reader the server says may send", async () => {
    mount(true);

    expect(await screen.findByRole("textbox", { name: /^message /i })).toBeTruthy();
    expect(screen.queryByText(READ_ONLY_DOCK_NOTE)).toBeNull();
  });
});

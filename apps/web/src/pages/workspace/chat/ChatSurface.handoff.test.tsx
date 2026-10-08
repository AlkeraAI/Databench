// A chat started from the home surface hands its first message over through nav
// state. The surface has to paint that message and the working indicator on the
// FIRST frame — the harness takes seconds to open, and a blank screen in the
// meantime reads as a message that went nowhere. It also must not fall back to
// the newest existing chat while it waits: the user is starting a new one.
//
// The handoff carries the picked model too, so the composer shows what was
// picked rather than whatever the catalog happens to list first.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const HANDED_OVER = "Migrate the orders model";
const OLD_REPLY = "an answer from the chat that was already here";

const { ds, state } = vi.hoisted(() => {
  const state = { models: [] as Record<string, unknown>[] };
  const ds = {
    // Chats DO exist: the handoff must still open a new one rather than land on
    // the newest of these.
    listChats: async () => [{ id: "already-here", title: "A chat that was already here", updatedAt: "2026-06-10T00:00:00Z" }],
    listModels: async () => state.models,
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: async () => [] as unknown[],
    listContext: async () => ({ total: 0, items: [] as unknown[] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] as unknown[] }),
    getChatTurns: vi.fn(async () => [
      { id: "old", author: "assistant", status: "done", parts: [{ id: "old-t", kind: "text", text: OLD_REPLY }] },
    ]),
    searchFiles: async () => [] as unknown[],
    subscribeChat: () => () => {},
    subscribePermissionMode: () => () => {},
    getPermissionMode: async () => "default",
    setPermissionMode: vi.fn(async () => {}),
    setEffort: vi.fn(async () => {}),
    // Never resolves: holds the surface in the optimistic state so it stays
    // assertable.
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

const FIRST_MODEL = { id: "model-alpha", displayName: "Model Alpha", efforts: [], defaultEffort: null };
const PICKED_MODEL = { id: "model-beta", displayName: "Model Beta", efforts: [], defaultEffort: null };

function renderHandoff(pendingOptions: unknown) {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[{ pathname: "/chat", state: { pendingMessage: HANDED_OVER, pendingOptions } }]}>
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
  state.models = [];
  vi.clearAllMocks();
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

describe("ChatSurface handoff from the home surface", () => {
  it("shows the handed-over message and the working indicator on the first paint", () => {
    renderHandoff(null);

    expect(screen.getByText(HANDED_OVER)).toBeInTheDocument();
    expect(screen.getByText(/working/i)).toBeInTheDocument();
    // No flash of the opening suggestions on the way.
    expect(screen.queryByRole("button", { name: new RegExp(DEFAULT_PROMPTS[0].label, "i") })).toBeNull();
  });

  it("opens a new chat instead of falling back to the newest existing one", async () => {
    renderHandoff(null);

    await waitFor(() => expect(ds.createChat).toHaveBeenCalledWith(HANDED_OVER, undefined));
    // No chat id yet, so no history was fetched and none is on screen.
    expect(ds.getChatTurns).not.toHaveBeenCalled();
    expect(screen.queryByText(OLD_REPLY)).toBeNull();
  });

  it("keeps the model picked before the handoff, not the catalog's first entry", async () => {
    state.models = [FIRST_MODEL, PICKED_MODEL];
    renderHandoff({ model: { id: PICKED_MODEL.id } });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^model:/i })).toHaveAccessibleName(`Model: ${PICKED_MODEL.displayName}`),
    );
  });
});

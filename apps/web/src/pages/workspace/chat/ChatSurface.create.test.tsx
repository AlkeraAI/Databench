// Starting a chat from a handed-off first message. The surface shows the
// message while the harness opens, then swaps in the created chat's real
// transcript under its own model. When the create fails there is no chat to
// fold the failure into, so the reason has to reach the transcript itself.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../../api/queryClient";

const REPLY = {
  id: "a1",
  author: "assistant",
  status: "done",
  completedAt: "2026-06-10T00:00:02Z",
  parts: [{ id: "a1-t", kind: "text", text: "Lineage mapped." }],
};

const { ds, state, created } = vi.hoisted(() => {
  // `chats` is the daemon's list: empty until the chat is created, then holding
  // exactly what createChat answered with.
  const state = { chats: [] as Record<string, unknown>[] };
  /** The chat the daemon answers a create with, optionally under a pinned model. */
  const created = (model?: Record<string, unknown>): Record<string, unknown> => {
    const chat = { id: "c1", title: "Build it", updatedAt: "2026-06-10T00:00:00Z", ...(model ? { model } : {}) };
    state.chats = [chat];
    return chat;
  };
  const ds = {
    listChats: vi.fn(async () => state.chats),
    listModels: async () => [
      { id: "model-alpha", displayName: "Model Alpha", efforts: ["low", "high"], defaultEffort: "low" },
      { id: "model-beta", displayName: "Model Beta", efforts: ["low", "high"], defaultEffort: "low" },
    ],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    // Only the real chat id has a transcript, so a fold proves the surface
    // asked for the CREATED chat and not the placeholder it was showing.
    getChatTurns: vi.fn(async (id: string) => (id === "c1" ? [REPLY] : [])),
    searchFiles: async () => [] as unknown[],
    sendUserMessage: async () => ({ id: "m1", role: "user", content: "x" }),
    getPermissionMode: async () => "default",
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    createChat: vi.fn(async () => created()),
  };
  return { ds, state, created };
});

vi.mock("./data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./data")>()),
  chatHost: () => ({
    kind: "vscode",
    engine: { request: async () => ({}) },
    runCommand: async () => {},
    openFile: async () => {},
    subscribe: () => () => {},
    workspacePath: () => null,
    account: () => ({ email: null, webAppUrl: null }),
    onAccountChange: () => () => {},
    auth: { openBrowser: async () => {}, getState: () => undefined, setState: () => {}, on: () => () => {}, ready: () => {} },
  }),
  chatData: () => ds,
  refetchWhileErrored: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
  chatCaps: () => ({ opencodeActive: true }),
}));

import { ChatSurface } from "./ChatSurface";
import { useChatStore } from "./chatStore";
import { createErrorTurn } from "./controller";

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="loc">{location.pathname}</div>;
}

/** What a failed create reads as, taken from the formatter the surface uses —
 *  a copy here would keep passing while the real wording drifted. */
function failureLine(rejection: unknown): string {
  const part = createErrorTurn(rejection).parts[0];
  if (part.kind !== "system") throw new Error("a failed create no longer reads as a system line");
  return part.text;
}

afterEach(() => {
  cleanup();
  state.chats = [];
  vi.clearAllMocks();
  ds.createChat.mockImplementation(async () => created());
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

function renderHandoff(pendingMessage = "Build it") {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[{ pathname: "/chat", state: { pendingMessage, pendingOptions: null } }]}>
        <Routes>
          <Route path="/chat" element={<ChatSurface />} />
          <Route path="/chat/:id" element={<ChatSurface />} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ChatSurface new chat from a handed-off message", () => {
  it("replaces the pending message with the created chat's transcript", async () => {
    renderHandoff();

    // The folded reply can only render once createChat resolved and the surface
    // moved onto the real chat.
    expect(await screen.findByText("Lineage mapped.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/chat/c1"));
    expect(ds.createChat).toHaveBeenCalledWith("Build it", undefined);
  });

  it("shows the created chat's own model rather than the catalog's first", async () => {
    // The harness pins a model when it opens the chat. The composer has to show
    // THAT one on the first paint of the new chat, not the catalog default it
    // was offering a moment earlier.
    ds.createChat.mockImplementationOnce(async () => created({ id: "model-beta", efforts: ["low", "high"], effort: "high" }));
    renderHandoff();

    expect(await screen.findByText("Lineage mapped.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Model: Model Beta" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Model: Model Alpha" })).toBeNull();
  });
});

// The host bridge serializes a rejection as {message, code?} — a plain object,
// never an Error — so each shape has to reach the reader as its own reason.
const FAILURES = [
  { name: "the reason the host gave", rejection: { message: "Open a folder before Alkera can start." } },
  { name: "that the session had expired", rejection: { message: "auth required (missing)", code: -32001 } },
  { name: "the standing advice when the rejection says nothing", rejection: undefined },
] as const;

describe("ChatSurface new chat that cannot be created", () => {
  it.each(FAILURES)("tells the reader $name", async ({ rejection }) => {
    ds.createChat.mockRejectedValueOnce(rejection);
    renderHandoff();

    expect(await screen.findByText(failureLine(rejection))).toBeInTheDocument();
  });
});

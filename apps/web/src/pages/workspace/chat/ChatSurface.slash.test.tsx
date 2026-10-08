// A line that starts with "/" is a command, not a message. Where it goes
// depends on who owns the result: a command whose result is a card the daemon
// persists is handed to the daemon verbatim, and one the editor performs
// (leaving the chat, switching the mode, compacting) runs here from the bare
// command, whatever the user typed after it.
//
// A word the registry does not know is not a command at all, so it reaches the
// agent as ordinary prose rather than failing silently.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const CHAT = { id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" };

// The daemon's own command vocabulary. `mode` is presented as a panel (the
// daemon owns its card), `compact`/`exit` are editor actions, `bypass` is a
// hidden mode shortcut, and `help` has no editor presentation at all.
const DAEMON_COMMANDS = [
  { name: "title", aliases: [], summary: "show or set the chat title", usage: "[(text)]", hidden: false, ui: true },
  { name: "usage", aliases: [], summary: "show credits left", usage: "[(window)]", hidden: false, ui: true },
  { name: "compact", aliases: [], summary: "summarize what happened so far", usage: "", hidden: false, ui: true },
  { name: "exit", aliases: ["quit"], summary: "leave this chat", usage: "", hidden: false, ui: true },
  { name: "mode", aliases: [], summary: "show or switch the permission mode", usage: "[(name)]", hidden: false, ui: false },
  { name: "bypass", aliases: [], summary: "jump to bypass", usage: "", hidden: true, ui: false },
  { name: "help", aliases: [], summary: "explain the commands", usage: "", hidden: false, ui: true },
];

const { ds, state } = vi.hoisted(() => {
  const state = { chats: [] as Record<string, unknown>[] };
  const ds = {
    listChats: async () => state.chats,
    listModels: async () => [
      { id: "model-alpha", displayName: "Model Alpha", efforts: [], defaultEffort: null },
    ],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: vi.fn(async () => [] as unknown[]),
    listContext: async () => ({ total: 0, items: [] as unknown[] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] as unknown[] }),
    getChatTurns: vi.fn(async () => [] as unknown[]),
    searchFiles: async () => [] as unknown[],
    subscribeChat: () => () => {},
    subscribePermissionMode: () => () => {},
    getPermissionMode: async () => "default",
    setPermissionMode: vi.fn(async () => {}),
    setEffort: vi.fn(async () => {}),
    runCommand: vi.fn(async () => ({ kind: "ok", command: null, payload: {}, message: null })),
    createChat: vi.fn(() => new Promise<never>(() => {})),
    sendUserMessage: vi.fn(async () => ({})),
  };
  return { ds, state };
});

vi.mock("./data", () => ({
  chatHost: () => ({
    // The editor's shell, so `/exit` backs out to the sidecar it routes.
    kind: "vscode" as const,
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

function renderAt(entry: string) {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/chat" element={<ChatSurface />} />
          <Route path="/chat/:id" element={<ChatSurface />} />
          <Route path="/sidecar" element={<h1>Where the chats live</h1>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function typeLine(line: string): Promise<void> {
  const box = await screen.findByRole("textbox", { name: /message/i });
  await act(async () => {
    fireEvent.change(box, { target: { value: line } });
  });
}

async function submit(line: string): Promise<void> {
  await typeLine(line);
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
  });
}

beforeEach(() => {
  state.chats = [CHAT];
  ds.listCommands.mockResolvedValue(DAEMON_COMMANDS);
});

afterEach(() => {
  cleanup();
  state.chats = [];
  vi.clearAllMocks();
  ds.listCommands.mockResolvedValue([] as unknown[]);
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

describe("ChatSurface slash commands", () => {
  it("hands a daemon-owned command over with everything the user typed", async () => {
    renderAt("/chat/c1");
    await submit("/mode plan");

    await waitFor(() => expect(ds.runCommand).toHaveBeenCalledWith("c1", "/mode plan"));
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
  });

  it("runs an editor-owned command from the bare command word", async () => {
    renderAt("/chat/c1");
    await submit("/compact right now");

    // The editor performs this one, so the daemon sees the command alone.
    await waitFor(() => expect(ds.runCommand).toHaveBeenCalledWith("c1", "/compact"));
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
  });

  it("leaves the chat on the exit command, sending nothing", async () => {
    renderAt("/chat/c1");
    await submit("/exit");

    expect(await screen.findByRole("heading", { name: /where the chats live/i })).toBeInTheDocument();
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(ds.runCommand).not.toHaveBeenCalled();
  });

  it("switches the permission mode on a mode shortcut, sending nothing", async () => {
    renderAt("/chat/c1");
    await submit("/bypass");

    await waitFor(() => expect(ds.setPermissionMode).toHaveBeenCalledWith("c1", "bypass"));
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(ds.runCommand).not.toHaveBeenCalled();
  });

  it("passes a line the registry does not know to the agent as prose", async () => {
    renderAt("/chat/c1");
    await submit("/summon a unicorn");

    await waitFor(() => expect(ds.sendUserMessage).toHaveBeenCalledWith("c1", "/summon a unicorn", expect.anything()));
    expect(ds.runCommand).not.toHaveBeenCalled();
  });

  it("browses the commands the editor can present, and no others", async () => {
    renderAt("/chat/c1");
    await typeLine("/");

    const menu = await screen.findByRole("listbox", { name: /slash commands/i });
    for (const name of ["title", "usage", "compact", "exit", "mode"]) {
      expect(within(menu).getByRole("option", { name: new RegExp(`^/${name}\\b`) })).toBeInTheDocument();
    }
    // A hidden shortcut stays out of the browsable list, and so does a command
    // the editor has no way to present.
    expect(within(menu).queryByRole("option", { name: /^\/bypass\b/ })).toBeNull();
    expect(within(menu).queryByRole("option", { name: /^\/help\b/ })).toBeNull();
  });

  it("offers no commands before the chat exists", async () => {
    // Every command needs a live session; a chat that has not started has none.
    state.chats = [];
    renderAt("/chat");
    await typeLine("/");

    expect(screen.queryByRole("button", { name: /slash commands/i })).toBeNull();
    expect(screen.queryByRole("listbox", { name: /slash commands/i })).toBeNull();
  });
});

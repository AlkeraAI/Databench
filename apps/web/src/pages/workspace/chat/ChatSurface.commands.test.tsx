// Slash lines typed into the shipping composer. A line the registry knows runs
// as a command -- an action here, a daemon-owned command there -- and never
// reaches the agent as prose; a line it does not know is an ordinary message.
// The menu offers what a reader can actually run, and a command the daemon
// recorded reads back as its own line in the transcript.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../../api/queryClient";

const THINKING_TURN = {
  id: "a1",
  author: "assistant",
  status: "done",
  completedAt: "2026-06-11T00:00:00Z",
  parts: [
    { id: "a1-think", kind: "thinking", text: "the trail it followed" },
    { id: "a1-text", kind: "text", text: "Four warehouses, all mapped." },
  ],
};

const { ds, state } = vi.hoisted(() => {
  const state = {
    turns: [] as unknown[],
    chatEffort: "low" as string | undefined,
    defaultEffort: "low",
    // The browser reading a workspace machine's transcript: no harness of its
    // own, so no model catalogue and no effort to choose.
    opencodeActive: true,
    catalog: true,
  };
  const ds = {
    listChats: async () => [
      {
        id: "c1",
        title: "Chat",
        updatedAt: "2026-06-10T00:00:00Z",
        permissionMode: "default",
        // A cloud chat carries no model: the box chose it, and the browser is
        // reading a transcript, not driving a harness.
        ...(state.catalog
          ? {
              model: {
                id: "model-alpha",
                efforts: ["none", "low", "medium", "high"],
                ...(state.chatEffort === undefined ? {} : { effort: state.chatEffort }),
              },
            }
          : {}),
      },
    ],
    listModels: async () =>
      state.catalog
        ? [
            {
              id: "model-alpha",
              displayName: "Model Alpha",
              efforts: ["none", "low", "medium", "high"],
              defaultEffort: state.defaultEffort,
            },
          ]
        : [],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    // The daemon's own vocabulary. The summaries are this fake daemon's
    // wording; only the names and the hidden flag carry meaning here.
    listCommands: vi.fn(async () => [
      { name: "title", aliases: [], summary: "a name for this chat", usage: "[(text)]", hidden: false, ui: true },
      { name: "usage", aliases: [], summary: "what has been spent", usage: "[(window)]", hidden: false, ui: true },
      { name: "compact", aliases: [], summary: "fold the conversation up", usage: "", hidden: false, ui: true },
      { name: "exit", aliases: ["quit"], summary: "back to the list", usage: "", hidden: false, ui: true },
      { name: "mode", aliases: [], summary: "which permissions apply", usage: "[(name)]", hidden: false, ui: false },
      { name: "yolo", aliases: [], summary: "a shortcut into bypass", usage: "", hidden: true, ui: false },
    ]),
    runCommand: vi.fn(async () => ({ kind: "ok", command: null, payload: {}, message: null })),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: vi.fn(async () => state.turns),
    searchFiles: async () => [] as unknown[],
    sendUserMessage: vi.fn(async () => ({ id: "m1", role: "user", content: "x" })),
    createChat: vi.fn(),
    getPermissionMode: async () => "default",
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
  };
  return { ds, state };
});

vi.mock("./data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./data")>()),
  chatHost: () => ({
    kind: "vscode",
    engine: { request: async () => ({}) },
    runCommand: async () => {},
    openFile: async () => {},
    // The real host relays every webview's broadcast through window messages.
    subscribe: (cb: (message: { type: string }) => void) => {
      const handler = (event: MessageEvent) => cb((event.data ?? {}) as { type: string });
      window.addEventListener("message", handler);
      return () => window.removeEventListener("message", handler);
    },
    workspacePath: () => null,
    account: () => ({ email: null, webAppUrl: null }),
    onAccountChange: () => () => {},
    auth: { openBrowser: async () => {}, getState: () => undefined, setState: () => {}, on: () => () => {}, ready: () => {} },
  }),
  chatData: () => ds,
  refetchWhileErrored: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
  chatCaps: () => ({ opencodeActive: state.opencodeActive }),
}));

import { ChatSurface } from "./ChatSurface";
import { useChatStore } from "./chatStore";

afterEach(() => {
  cleanup();
  state.turns = [];
  state.chatEffort = "low";
  state.defaultEffort = "low";
  state.opencodeActive = true;
  state.catalog = true;
  vi.clearAllMocks();
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

function renderChat() {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:id" element={<ChatSurface />} />
          <Route path="/sidecar" element={<p>Every chat</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function type(text: string): Promise<void> {
  const box = await screen.findByRole("textbox", { name: /^message /i });
  await act(async () => {
    fireEvent.change(box, { target: { value: text } });
  });
}

async function submit(text: string): Promise<void> {
  await type(text);
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
  });
}

describe("ChatSurface slash lines", () => {
  it("runs an action command instead of sending it to the agent", async () => {
    renderChat();
    await submit("/compact");

    await waitFor(() => expect(ds.runCommand).toHaveBeenCalledWith("c1", "/compact"));
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(screen.queryByText("/compact")).toBeNull();
  });

  it("leaves the chat on an exit line", async () => {
    renderChat();
    await submit("/exit");

    expect(await screen.findByText("Every chat")).toBeInTheDocument();
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(ds.runCommand).not.toHaveBeenCalled();
  });

  it("hands a daemon-owned command to the daemon, not the agent", async () => {
    renderChat();
    await submit("/usage");

    await waitFor(() => expect(ds.runCommand).toHaveBeenCalledWith("c1", "/usage"));
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(screen.queryByText("/usage")).toBeNull();
  });

  it("sends a line no command answers to as an ordinary message", async () => {
    renderChat();
    await submit("/nope");

    await waitFor(() => expect(screen.getByText("/nope")).toBeInTheDocument());
    expect(ds.sendUserMessage.mock.calls[0]?.slice(0, 2)).toEqual(["c1", "/nope"]);
    expect(ds.runCommand).not.toHaveBeenCalled();
  });

  it("offers the commands it can run and keeps the hidden shortcuts out", async () => {
    renderChat();
    await waitFor(() => expect(ds.listCommands).toHaveBeenCalled());
    await type("/");

    const menu = await screen.findByRole("listbox", { name: "Slash commands" });
    for (const command of ["/title", "/usage", "/compact", "/exit", "/mode"]) {
      expect(within(menu).getByRole("option", { name: new RegExp(`^\\${command}\\b`) })).toBeInTheDocument();
    }
    expect(within(menu).queryByRole("option", { name: /^\/yolo\b/ })).toBeNull();
  });

  it("reads a recorded command back as its own transcript line", async () => {
    state.turns = [
      {
        id: "cmd1",
        author: "system",
        status: "done",
        parts: [
          {
            id: "cmd1-p",
            kind: "command",
            command: "clear",
            label: "History cleared",
            detail: "The next turn starts over.",
          },
        ],
      },
    ];
    renderChat();

    expect(await screen.findByText("History cleared")).toBeInTheDocument();
    expect(screen.getByText("/clear")).toBeInTheDocument();
    expect(screen.getByText("The next turn starts over.")).toBeInTheDocument();
  });
});

describe("ChatSurface transcript reasoning", () => {
  it("hides reasoning only for none, catching a blanket explicit-effort filter", async () => {
    state.turns = [THINKING_TURN];

    for (const [effort, visible] of [
      ["none", false],
      ["low", true],
      ["medium", true],
      ["high", true],
    ] as const) {
      state.chatEffort = effort;
      const view = renderChat();
      expect(await screen.findByText("Four warehouses, all mapped.")).toBeInTheDocument();

      const disclosure = screen.queryByRole("button", { name: /^thought for/i });
      if (visible) {
        expect(disclosure).toBeInTheDocument();
        await act(async () => {
          fireEvent.click(disclosure!);
        });
        expect(screen.getByText("the trail it followed")).toBeInTheDocument();
      } else {
        expect(disclosure).toBeNull();
        expect(screen.queryByText("the trail it followed")).toBeNull();
      }
      view.unmount();
    }
  });

  it("shows the machine's reasoning in a shell with no harness of its own", async () => {
    // The browser against a workspace box: no opencode here, so no catalogue
    // and no effort to resolve. The rule that hides reasoning at effort "none"
    // must not fire on that unresolved catalogue and silently delete every
    // reasoning part the machine already published.
    state.turns = [THINKING_TURN];
    state.opencodeActive = false;
    state.catalog = false;

    renderChat();
    expect(await screen.findByText("Four warehouses, all mapped.")).toBeInTheDocument();
    const disclosure = screen.getByRole("button", { name: /^thought for/i });
    await act(async () => {
      fireEvent.click(disclosure);
    });
    expect(screen.getByText("the trail it followed")).toBeInTheDocument();
  });

  it("uses the catalog default for omitted effort, catching a missing-effort-as-none fallback", async () => {
    state.turns = [THINKING_TURN];
    state.chatEffort = undefined;

    for (const [defaultEffort, visible] of [
      ["low", true],
      ["none", false],
    ] as const) {
      state.defaultEffort = defaultEffort;
      const view = renderChat();
      expect(await screen.findByText("Four warehouses, all mapped.")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^thought for/i }) !== null).toBe(visible);
      view.unmount();
    }
  });
});

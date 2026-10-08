// The chat family's shipping home: what the list shows, what a row's controls
// reach, what a delete costs, and what the surface says when the daemon cannot
// answer. The sidecar's suite is where these behaviors were first pinned -- both
// homes now run on the same list hook -- so the same contracts are asserted
// here, against the surface a route actually mounts.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const ROW_TIME = "2026-06-10T00:00:00Z";

/** The row's controls and marks are addressed the way a screen reader hears
 *  them. The package keeps these labels private, so they are spelled here. */
const WORKING_DOT = "The agent is working"; // pins-source: the accessible name a reader hears; @alkera/ui exports no constant for it
const actionsFor = (title: string): string => `Actions for ${title}`; // pins-source: same accessible-name contract, one per row

const { ds, runCommand } = vi.hoisted(() => {
  const ds = {
    listChats: vi.fn(async () => [] as unknown[]),
    listModels: vi.fn(async () => [
      { id: "model-alpha", displayName: "Model Alpha", efforts: ["low", "high"], defaultEffort: "low" },
    ] as unknown[]),
    resolveChatDefaults: vi.fn(async () => ({ model: null, effort: null })),
    listCommands: vi.fn(async () => [] as unknown[]),
    // Home hands its first message to the chat route; it must never open a
    // daemon session itself.
    createChat: vi.fn(async () => ({ id: "unused", title: "x", updatedAt: ROW_TIME })),
    deleteChat: vi.fn(async () => true),
    getChatActivity: vi.fn(() => ({}) as Record<string, unknown>),
    getSubagentChats: vi.fn(() => [] as unknown[]),
    getSubagentLabels: vi.fn(() => ({}) as Record<string, string>),
    subscribeChat: vi.fn(() => () => {}),
    listContext: vi.fn(async () => ({ items: [], total: 0 })),
    lineageRoots: vi.fn(async () => ({ nodes: [], total_nodes: 0 })),
    searchFiles: vi.fn(async () => [] as unknown[]),
  };
  const runCommand = vi.fn(async () => {});
  return { ds, runCommand };
});

// The module's own wording helpers stay real -- a local copy of `errorText`
// would keep passing while the product's phrasing drifted. Only the data source
// and the poll intervals are swapped.
vi.mock("./data", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./data")>();
  return {
    ...actual,
    chatData: () => ds,
    // The extension's source drives an OpenCode harness, which is what home's
    // engine-shaped reads (models, defaults) gate on.
    chatCaps: () => ({ opencodeActive: true }),
  chatHost: () => ({
    kind: "vscode",
    runCommand,
    openFile: async () => {},
    subscribe: () => () => {},
    workspacePath: () => null,
    account: () => ({ email: null, webAppUrl: null }),
    onAccountChange: () => () => {},
    engine: { request: async () => ({}) },
    // The chrome's auth hook consumes the whole channel; a stub with no cache
    // and no pushes leaves it signed out, which home renders fine.
    auth: {
      openBrowser: async () => {},
      getState: () => undefined,
      setState: () => {},
      on: () => () => {},
      ready: () => {},
    },
  }),

    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
  };
});


import { ChatHomeSurface } from "./ChatHomeSurface";
import { useChatActivity } from "./activityStore";
import { useChatStore } from "./chatStore";

/** Stands in for the chat route: reports where home sent the reader and what it
 *  carried. */
function RouteProbe() {
  const location = useLocation();
  return <div data-testid="route">{`${location.pathname}|${JSON.stringify(location.state)}`}</div>;
}

function renderHome() {
  const client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<ChatHomeSurface />} />
          <Route path="/chat" element={<RouteProbe />} />
          <Route path="/chat/:chatId" element={<RouteProbe />} />
          <Route path="/editor/chat/:chatId" element={<RouteProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Open a row's menu and pick its destructive row. The host owns the native
 *  confirm modal, so what the webview can prove is the ask and what follows it. */
async function askToDelete(title: string) {
  const trigger = await screen.findByRole("button", { name: actionsFor(title) });
  await act(async () => {
    fireEvent.click(trigger);
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem", { name: /delete chat/i }));
  });
}

/** Type a line into the home composer and send it. */
async function sendLine(text: string) {
  const box = await screen.findByRole("textbox", { name: /message/i });
  await act(async () => {
    fireEvent.change(box, { target: { value: text } });
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
  });
}

/** The row a chat's title leads. Rows are list items; the title is the door's
 *  accessible name. */
function rowFor(title: string): HTMLElement {
  const row = screen.getAllByRole("listitem").find((item) => item.textContent?.includes(title));
  if (!row) throw new Error(`no row leads with ${title}`);
  return row;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  ds.listChats.mockResolvedValue([]);
  ds.listModels.mockResolvedValue([
    { id: "model-alpha", displayName: "Model Alpha", efforts: ["low", "high"], defaultEffort: "low" },
  ]);
  ds.listCommands.mockResolvedValue([]);
  ds.deleteChat.mockResolvedValue(true);
  ds.getChatActivity.mockReturnValue({});
  ds.getSubagentChats.mockReturnValue([]);
  ds.getSubagentLabels.mockReturnValue({});
  useChatStore.setState({ byId: {}, composerPrefs: {} });
  useChatActivity.setState({ activity: {}, subagents: [], labels: {} });
});

describe("the chats a home lists", () => {
  it("shows every chat the daemon listed", async () => {
    ds.listChats.mockResolvedValue([
      { id: "chat-7", title: "Fix the parser", updatedAt: ROW_TIME },
      { id: "chat-8", title: "Draft the mart", updatedAt: ROW_TIME },
    ]);
    renderHome();

    expect(await screen.findByRole("button", { name: "Fix the parser" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Draft the mart" })).toBeInTheDocument();
  });

  it("says the workspace holds no chats rather than showing an empty frame", async () => {
    ds.listChats.mockResolvedValue([]);
    renderHome();

    expect(await screen.findByText(/no chats yet/i)).toBeInTheDocument();
    const identity = screen.getByRole("heading", { name: "Chats" }).closest(".chat-chrome__id");
    expect(identity?.querySelector("svg")).toBeNull();
  });

  it("holds the composer back until the daemon has answered", async () => {
    // An empty list under a live composer would read as the truth before the
    // daemon has said anything.
    let answer: (chats: unknown[]) => void = () => {};
    ds.listChats.mockImplementation(
      () => new Promise<unknown[]>((resolve) => { answer = resolve; }),
    );
    renderHome();

    expect(await screen.findByText(/loading chats/i)).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /message/i })).toBeNull();

    await act(async () => {
      answer([]);
    });

    expect(await screen.findByRole("textbox", { name: /message/i })).toBeInTheDocument();
  });

  it("opens the chat a reader picks", async () => {
    ds.listChats.mockResolvedValue([{ id: "chat-7", title: "Fix the parser", updatedAt: ROW_TIME }]);
    renderHome();

    const door = await screen.findByRole("button", { name: "Fix the parser" });
    await act(async () => {
      fireEvent.click(door);
    });

    expect(await screen.findByTestId("route")).toHaveTextContent("/chat/chat-7");
  });

  it("hangs a spawned chat under the chat that spawned it, named by its parent", async () => {
    ds.listChats.mockResolvedValue([{ id: "chat-7", title: "Fix the parser", updatedAt: ROW_TIME }]);
    ds.getSubagentChats.mockReturnValue([
      { id: "chat-9", title: "session 9", updatedAt: ROW_TIME, parentSessionId: "chat-7" },
    ]);
    ds.getSubagentLabels.mockReturnValue({ "chat-9": "Check the schema" });
    renderHome();

    const disclosure = await screen.findByRole("button", { name: /show 1 subagent chat/i });
    expect(screen.queryByRole("button", { name: "Check the schema" })).toBeNull();

    await act(async () => {
      fireEvent.click(disclosure);
    });

    expect(screen.getByRole("button", { name: "Check the schema" })).toBeInTheDocument();
  });
});

describe("how fresh a home row says its chat is", () => {
  it("ages a chat by the clock the daemon reported", async () => {
    const fiveMinutesAgo = new Date(Date.now() - 5 * 60_000).toISOString();
    ds.listChats.mockResolvedValue([
      { id: "chat-7", title: "Fix the parser", updatedAt: fiveMinutesAgo },
    ]);
    renderHome();

    await screen.findByRole("button", { name: "Fix the parser" });
    expect(rowFor("Fix the parser")).toHaveTextContent("5m");
  });

  it.each([
    { case: "a chat the daemon timestamped with nothing at all", updatedAt: undefined },
    { case: "a chat whose timestamp came back empty", updatedAt: "" },
    { case: "a chat whose timestamp cannot be read as a date", updatedAt: "shortly" },
  ])("$case still lists, with no clock beside it", async ({ updatedAt }) => {
    ds.listChats.mockResolvedValue([{ id: "chat-7", title: "Fix the parser", updatedAt }]);
    renderHome();

    await screen.findByRole("button", { name: "Fix the parser" });
    expect(rowFor("Fix the parser").textContent).toBe("Fix the parser");
  });
});

describe("what a home row says its chat is doing", () => {
  it("lights the working dot for the chat the fold reports as awaiting", async () => {
    ds.listChats.mockResolvedValue([
      { id: "chat-7", title: "Busy one", updatedAt: ROW_TIME },
      { id: "chat-8", title: "Quiet one", updatedAt: ROW_TIME },
    ]);
    ds.getChatActivity.mockReturnValue({
      "chat-7": { awaiting: true, lastInteractionAt: null },
    });
    renderHome();

    const dots = await screen.findAllByRole("img", { name: WORKING_DOT });
    expect(dots).toHaveLength(1);
    expect(rowFor("Busy one")).toContainElement(dots[0]);
  });

  it("clears the dot when the fold says the background turn finished", async () => {
    ds.listChats.mockResolvedValue([{ id: "chat-7", title: "Backgrounded", updatedAt: ROW_TIME }]);
    // The fold kept consuming events while no route was mounted: the turn ended.
    ds.getChatActivity.mockReturnValue({
      "chat-7": { awaiting: false, lastInteractionAt: "2026-06-11T01:00:00Z" },
    });
    // The store entry froze at sending=true when its route unmounted.
    useChatStore.setState({
      byId: {
        "chat-7": {
          base: [],
          optimistic: [],
          baseUserCount: 0,
          sending: true,
          loading: false,
          error: null,
          staleSend: null,
          sendRefusal: null,
          returnedDraft: null,
          released: false,
          queued: [],
          queuedBehind: null,
        },
      },
    });
    renderHome();

    await screen.findByRole("button", { name: "Backgrounded" });
    expect(screen.queryByRole("img", { name: WORKING_DOT })).not.toBeInTheDocument();
  });

  it("lights the dot from this window's own send before any event lands", async () => {
    ds.listChats.mockResolvedValue([{ id: "chat-8", title: "Just sent", updatedAt: ROW_TIME }]);
    ds.getChatActivity.mockReturnValue({});
    useChatStore.setState({
      byId: {
        "chat-8": {
          base: [],
          optimistic: [],
          baseUserCount: 0,
          sending: true,
          loading: false,
          error: null,
          staleSend: null,
          sendRefusal: null,
          returnedDraft: null,
          released: false,
          queued: [],
          queuedBehind: null,
        },
      },
    });
    renderHome();

    expect(await screen.findByRole("img", { name: WORKING_DOT })).toBeInTheDocument();
  });

  it("names what a stopped chat is waiting on the reader for", async () => {
    ds.listChats.mockResolvedValue([{ id: "chat-7", title: "Stopped one", updatedAt: ROW_TIME }]);
    ds.getChatActivity.mockReturnValue({
      "chat-7": { awaiting: true, lastInteractionAt: null, ask: "permission" },
    });
    renderHome();

    expect(await screen.findByRole("img", { name: /waiting for permission/i })).toBeInTheDocument();
  });
});

describe("reaching a home row's controls from the keyboard", () => {
  it("names every control on a row, and each one takes focus", async () => {
    ds.listChats.mockResolvedValue([{ id: "chat-7", title: "Fix the parser", updatedAt: ROW_TIME }]);
    ds.getSubagentChats.mockReturnValue([
      { id: "chat-9", title: "session 9", updatedAt: ROW_TIME, parentSessionId: "chat-7" },
    ]);
    renderHome();

    const door = await screen.findByRole("button", { name: "Fix the parser" });
    const disclosure = screen.getByRole("button", { name: /show 1 subagent chat/i });
    const actions = screen.getByRole("button", { name: actionsFor("Fix the parser") });

    for (const control of [door, disclosure, actions]) {
      control.focus();
      expect(document.activeElement).toBe(control);
    }
  });

  it("hands focus to the first action when the menu opens, and back when it closes", async () => {
    ds.listChats.mockResolvedValue([{ id: "chat-7", title: "Fix the parser", updatedAt: ROW_TIME }]);
    renderHome();

    const trigger = await screen.findByRole("button", { name: actionsFor("Fix the parser") });
    await act(async () => {
      fireEvent.click(trigger);
    });

    const first = screen.getByRole("menuitem", { name: /open chat/i });
    expect(document.activeElement).toBe(first);

    await act(async () => {
      fireEvent.keyDown(first, { key: "Escape" });
    });

    expect(document.activeElement).toBe(trigger);
    expect(screen.queryByRole("menuitem", { name: /delete chat/i })).toBeNull();
  });
});

describe("deleting a chat from home", () => {
  it("asks by chat id and title, and drops the row once the daemon confirms", async () => {
    // The first read lists the chat; after the daemon removes it the refetch is empty.
    ds.listChats
      .mockResolvedValueOnce([{ id: "chat-7", title: "Fix the parser", updatedAt: ROW_TIME }])
      .mockResolvedValue([]);
    ds.deleteChat.mockResolvedValue(true);
    renderHome();

    await askToDelete("Fix the parser");

    expect(ds.deleteChat).toHaveBeenCalledWith("chat-7", "Fix the parser");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Fix the parser" })).toBeNull());
  });

  it("keeps the row when the reader cancels the confirm", async () => {
    ds.listChats.mockResolvedValue([{ id: "chat-7", title: "Keep me", updatedAt: ROW_TIME }]);
    ds.deleteChat.mockResolvedValue(false);
    renderHome();

    await askToDelete("Keep me");

    expect(ds.deleteChat).toHaveBeenCalledWith("chat-7", "Keep me");
    // Give any wrongful refresh a full tick to land, then prove the list was
    // never re-read: a cancelled confirm must not invalidate.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(ds.listChats).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Keep me" })).toBeInTheDocument();
  });

  it("keeps the daemon's reason readable when a delete fails, and keeps the row", async () => {
    ds.listChats.mockResolvedValue([{ id: "chat-7", title: "Locked one", updatedAt: ROW_TIME }]);
    ds.deleteChat.mockRejectedValue({ message: "chat is locked by pid 4242" });
    renderHome();

    await askToDelete("Locked one");

    expect(await screen.findByText(/couldn't delete the chat/i)).toBeInTheDocument();
    expect(screen.getByText(/chat is locked by pid 4242/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Locked one" })).toBeInTheDocument();
  });

  it("drops a stale failure the moment a retry works", async () => {
    ds.listChats.mockResolvedValue([{ id: "chat-7", title: "Locked one", updatedAt: ROW_TIME }]);
    ds.deleteChat.mockRejectedValueOnce({ message: "chat is locked by pid 4242" });
    renderHome();

    await askToDelete("Locked one");
    await screen.findByText(/chat is locked by pid 4242/);

    ds.deleteChat.mockResolvedValue(true);
    ds.listChats.mockResolvedValue([]);
    await askToDelete("Locked one");

    await waitFor(() => expect(screen.queryByText(/chat is locked by pid 4242/)).toBeNull());
  });
});

describe("what home says when the daemon cannot answer", () => {
  it("names the reason the workspace would not load", async () => {
    ds.listChats.mockRejectedValue({ message: "daemon not connected" });
    renderHome();

    expect(await screen.findByText(/chats unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/daemon not connected/)).toBeInTheDocument();
  });

  it("offers no composer while the workspace cannot be read", async () => {
    ds.listChats.mockRejectedValue({ message: "daemon not connected" });
    renderHome();

    await screen.findByText(/chats unavailable/i);
    expect(screen.queryByRole("textbox", { name: /message/i })).toBeNull();
  });

  it("names the reason the model catalog would not load", async () => {
    ds.listModels.mockRejectedValue({ message: "gateway said 502" });
    renderHome();

    expect(await screen.findByText(/models unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/gateway said 502/)).toBeInTheDocument();
  });

  it("says the gateway offered nothing rather than showing an empty picker", async () => {
    ds.listModels.mockResolvedValue([]);
    renderHome();

    expect(await screen.findByText(/no models available/i)).toBeInTheDocument();
  });
});

describe("starting a chat from home", () => {
  it("carries the first message to the chat route without opening a session first", async () => {
    renderHome();

    await sendLine("Build the mart");

    const route = await screen.findByTestId("route");
    expect(route).toHaveTextContent("/chat");
    expect(route).toHaveTextContent("Build the mart");
    expect(ds.createChat).not.toHaveBeenCalled();
  });

  it("runs an account-level command here instead of starting a chat named after it", async () => {
    ds.listCommands.mockResolvedValue([
      { name: "preferences", aliases: [], summary: "Open preferences", usage: "", hidden: false, ui: true },
    ]);
    renderHome();

    await sendLine("/preferences");

    expect(runCommand).toHaveBeenCalledWith({ command: "alkera.openPreferences" });
    expect(screen.queryByTestId("route")).toBeNull();
  });

  it("explains that a chat-only command needs a chat, and starts none", async () => {
    renderHome();

    await sendLine("/mode plan");

    expect(await screen.findByText(/that command runs inside a chat/i)).toBeInTheDocument();
    expect(screen.queryByTestId("route")).toBeNull();
  });

  it("lets the reader dismiss that explanation", async () => {
    renderHome();

    await sendLine("/mode plan");
    await screen.findByText(/that command runs inside a chat/i);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    });

    expect(screen.queryByText(/that command runs inside a chat/i)).toBeNull();
  });
});

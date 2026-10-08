// The model and effort chips on an OPEN cloud chat, in a browser tab.
//
// An open chat may move to another model, but never to one that cannot read the
// reasoning its history already carries, and an effort change
// on the same model always goes through. The server decides each model's
// verdict; the picker shows them verbatim: a model the chat may move to is
// picked as before, one it may not is greyed with the reason and offers the way
// out — "Start a new chat with <model>", which opens the empty composer on that
// model with the reader's unsent words carried over.
//
// The capabilities are the cloud source's REAL ones (`new CloudDataSource().caps`),
// so the day the gate is turned off again this file says so.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const FABLE = {
  id: "claude-fable-5",
  displayName: "Claude Fable 5",
  wire: "anthropic",
  efforts: ["low", "medium", "high"],
  defaultEffort: "medium",
};
const OPUS = {
  id: "claude-opus-5.5",
  displayName: "Claude Opus 5.5",
  wire: "anthropic",
  efforts: ["low", "high"],
  defaultEffort: "high",
};
const GPT = {
  id: "gpt-5.5",
  displayName: "GPT-5.5",
  wire: "openai",
  efforts: [],
  defaultEffort: null,
};
const REFUSAL =
  "This chat has reasoning from Claude Fable 5 that GPT-5.5 can't read. Start a new chat to use GPT-5.5.";
const GROUP = "This chat has reasoning from Claude Fable 5 that these models can't read. Use them in a new chat.";
const MINI = {
  id: "gpt-5.5-mini",
  displayName: "GPT-5.5 mini",
  wire: "openai",
  efforts: [],
  defaultEffort: null,
};

const { ds, setModel, setEffort, options } = vi.hoisted(() => {
  const setModel = vi.fn(async (..._args: unknown[]) => {});
  const setEffort = vi.fn(async (_chatId: string, _effort: string) => {});
  const options = {
    canSwitch: true,
    applies: "next_turn" as "next_turn" | "after_reopen",
    billedToOwner: false,
    // The chat moving on (a turn landing) and what its history now rules out.
    updatedAt: "2026-09-06T12:00:00Z",
    opusRefused: false,
  };
  return { setModel, setEffort, options, ds: {} as Record<string, unknown> };
});

Object.assign(ds, {
  listChats: async () => [
    {
      id: "c1",
      title: "Ops",
      updatedAt: options.updatedAt,
      permissionMode: "read_only",
      model: { id: "claude-fable-5", efforts: ["low", "medium", "high"], effort: "medium" },
    },
  ],
  listModels: async () => [FABLE, OPUS, GPT, MINI],
  modelOptions: async () => ({
    currentModelId: "claude-fable-5",
    currentEffort: "medium",
    canSwitch: options.canSwitch,
    applies: options.applies,
    billedToOwner: options.billedToOwner,
    options: [
      { model: FABLE, state: "current", reasonCode: null, message: null, escapeNewChatModel: null },
      options.opusRefused
        ? {
            model: OPUS,
            state: "unavailable",
            reasonCode: "reasoning_not_readable",
            message: "no",
            groupMessage: GROUP,
            escapeNewChatModel: "claude-opus-5.5",
          }
        : { model: OPUS, state: "available", reasonCode: null, message: null, escapeNewChatModel: null },
      {
        model: GPT,
        state: "unavailable",
        reasonCode: "reasoning_not_readable",
        message: REFUSAL,
        groupMessage: GROUP,
        escapeNewChatModel: "gpt-5.5",
      },
      {
        model: MINI,
        state: "unavailable",
        reasonCode: "reasoning_not_readable",
        message: "This chat has reasoning from Claude Fable 5 that GPT-5.5 mini can't read.",
        groupMessage: GROUP,
        escapeNewChatModel: "gpt-5.5-mini",
      },
    ],
  }),
  resolveChatDefaults: async () => ({ model: "claude-fable-5", effort: "medium" }),
  listCommands: async () => [],
  runCommand: async () => ({ kind: "cli_only", command: null, payload: {}, message: "" }),
  listContext: async () => ({ total: 0 }),
  lineageRoots: async () => ({}),
  getChatTurns: async () => [],
  searchFiles: async () => [],
  sendUserMessage: async () => ({ id: "m1", role: "user", content: "x" }),
  createChat: async () => ({ id: "c1", title: null, updatedAt: "" }),
  getPermissionMode: async () => "read_only",
  setPermissionMode: async () => {},
  setModel,
  setEffort,
  subscribePermissionMode: () => () => {},
  subscribeChat: () => () => {},
  getChatActivity: () => ({}),
  getSubagentChats: () => [],
  getSubagentLabels: () => ({}),
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const { CloudDataSource } = await import("@/pages/workspace/chat/data/CloudDataSource");
  const host = real.createBrowserChatHost({
    account: () => ({ email: "analyst@tideline.example", webAppUrl: null }),
  });
  const caps = new CloudDataSource().caps;
  return {
    ...real,
    chatHost: () => host,
    chatData: () => ds,
    chatCaps: () => caps,
    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
  };
});

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";
import { chatKeys } from "@/pages/workspace/chat/chatKeys";
import { useChatStore } from "@/pages/workspace/chat/chatStore";

beforeEach(() => {
  options.canSwitch = true;
  options.applies = "next_turn";
  options.billedToOwner = false;
  options.updatedAt = "2026-09-06T12:00:00Z";
  options.opusRefused = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
    ),
  );
});

afterEach(() => {
  cleanup();
  // A new-chat pick lives in the store, which outlives one render.
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
  window.localStorage.clear();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

let client = createQueryClient({ retry: false });

/** Where the router is, for the test to read. */
function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

function renderAt(path: string, workspaceId?: string): void {
  client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Where />
        <Routes>
          <Route path="/chat/:chatId" element={<ChatSurface chatId="c1" workspaceId={workspaceId} />} />
          <Route path="/chat/new" element={<ChatSurface />} />
          <Route path="/chat" element={<ChatSurface />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const chip = (category: "Model" | "Effort"): Promise<HTMLButtonElement> =>
  screen.findByRole("button", { name: new RegExp(`^${category}: `) }) as Promise<HTMLButtonElement>;

async function openModelMenu(): Promise<HTMLElement> {
  // The verdicts land after the chat does; open once the refused row is there.
  await waitFor(async () => {
    fireEvent.click(await chip("Model"));
    const menu = await screen.findByRole("listbox", { name: "Model" });
    expect(within(menu).getByText(GROUP)).toBeTruthy();
  });
  return screen.getByRole("listbox", { name: "Model" });
}

describe("the model picker on an open chat", () => {
  it("moves the chat to a model that reads its history, saying what it switched from", async () => {
    renderAt("/chat/c1");
    const menu = await openModelMenu();

    fireEvent.click(within(menu).getByText("Claude Opus 5.5"));

    await waitFor(() =>
      expect(setModel).toHaveBeenCalledWith("c1", "claude-opus-5.5", undefined, "claude-fable-5"),
    );
  });

  it("lists every model the reasoning rules out under one line, after the ones it may move to", async () => {
    renderAt("/chat/c1");
    const menu = await openModelMenu();

    expect(within(menu).getAllByText(GROUP)).toHaveLength(1);
    expect(within(menu).queryByText(REFUSAL)).toBeNull();
    const order = within(menu)
      .getAllByRole("option")
      .map((row) => row.textContent ?? "");
    expect(order.map((text) => text.replace("New chat", ""))).toEqual([
      "Claude Fable 5",
      "Claude Opus 5.5",
      "GPT-5.5",
      "GPT-5.5 mini",
    ]);
  });

  it("never moves the chat onto a model the reasoning rules out", async () => {
    renderAt("/chat/c1");
    const menu = await openModelMenu();

    fireEvent.click(within(menu).getByRole("option", { name: "Start a new chat with GPT-5.5" }));

    expect(setModel).not.toHaveBeenCalled();
  });

  it("offers the way out as a row of its own, reachable from the keyboard", async () => {
    renderAt("/chat/c1");
    const field = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    fireEvent.change(field, { target: { value: "rerun it on the bigger table" } });
    const menu = await openModelMenu();
    const way = within(menu).getByRole("option", { name: "Start a new chat with GPT-5.5" });
    expect(way.getAttribute("aria-disabled")).toBeNull();
    expect(way.tagName).toBe("BUTTON");

    fireEvent.click(way);

    await waitFor(async () =>
      expect((await chip("Model")).getAttribute("aria-label")).toBe("Model: GPT-5.5"),
    );
    expect(((await screen.findByRole("textbox")) as HTMLTextAreaElement).value).toBe(
      "rerun it on the bigger table",
    );
    expect(setModel).not.toHaveBeenCalled();
  });

  it("starts the new chat in the open chat's workspace", async () => {
    renderAt("/chat/c1", "ws-alpha");
    const menu = await openModelMenu();

    fireEvent.click(within(menu).getByRole("option", { name: "Start a new chat with GPT-5.5" }));

    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/chat/new?workspace=ws-alpha"));
  });

  it("starts the new chat where a new chat always goes when the open one names no workspace", async () => {
    renderAt("/chat/c1");
    const menu = await openModelMenu();

    fireEvent.click(within(menu).getByRole("option", { name: "Start a new chat with GPT-5.5" }));

    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/chat/new"));
  });

  it("says a switch waits for a restart on a box that cannot move a running agent", async () => {
    options.applies = "after_reopen";
    renderAt("/chat/c1");
    const menu = await openModelMenu();

    expect(menu.parentElement?.parentElement?.textContent).toContain(
      "Applies after the chat restarts.",
    );
  });

  it("tells a reader who is not the owner that the list is the owner's plan", async () => {
    options.billedToOwner = true;
    renderAt("/chat/c1");
    const menu = await openModelMenu();

    expect(menu.parentElement?.parentElement?.textContent).toContain(
      "Models on the chat owner's plan.",
    );
  });

  it("says nothing about plans to the owner", async () => {
    renderAt("/chat/c1");
    const menu = await openModelMenu();

    expect(menu.parentElement?.parentElement?.textContent).not.toContain("owner's plan");
  });

  it("reads the verdicts again when a turn lands, so it never offers what the history now rules out", async () => {
    renderAt("/chat/c1");
    fireEvent.click(await chip("Model"));
    const before = await screen.findByRole("listbox", { name: "Model" });
    await waitFor(() => expect(within(before).getByText(GROUP)).toBeTruthy());
    expect(within(before).getByRole("option", { name: /Claude Opus 5\.5/ }).getAttribute("aria-label")).toBeNull();

    options.opusRefused = true;
    options.updatedAt = "2026-09-06T12:05:00Z";
    await client.invalidateQueries({ queryKey: chatKeys.chats() });

    await waitFor(() =>
      expect(
        within(screen.getByRole("listbox", { name: "Model" })).getByRole("option", {
          name: "Start a new chat with Claude Opus 5.5",
        }),
      ).toBeTruthy(),
    );
  });

  it("is a readout for a reader who may not switch", async () => {
    options.canSwitch = false;
    renderAt("/chat/c1");

    await waitFor(async () => expect((await chip("Model")).getAttribute("aria-haspopup")).toBeNull());
  });

  it("changes the effort on the model the chat is on", async () => {
    renderAt("/chat/c1");

    fireEvent.click(await chip("Effort"));
    const menu = await screen.findByRole("listbox", { name: "Effort" });
    fireEvent.click(within(menu).getByText(/high/i));

    await waitFor(() => expect(setEffort).toHaveBeenCalledWith("c1", "high"));
  });
});

describe("the same chips on the home composer, before a chat exists", () => {
  it.each([["Model"], ["Effort"]] as const)("opens the %s picker", async (category) => {
    renderAt("/chat");

    const control = await chip(category);
    expect(control.disabled).toBe(false);
    fireEvent.click(control);
    expect(await screen.findByRole("listbox", { name: category })).toBeTruthy();
  });

  it("offers the whole catalogue, with no verdicts, to pick the next chat's model from", async () => {
    renderAt("/chat");

    fireEvent.click(await chip("Model"));
    const menu = await screen.findByRole("listbox", { name: "Model" });
    expect(menu.textContent).toContain("GPT-5.5");
    expect(menu.textContent).not.toContain(REFUSAL);
  });
});

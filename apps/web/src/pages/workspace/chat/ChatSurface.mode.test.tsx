// The permission mode on the shipping surface: what the pill shows before the
// daemon's authoritative read lands, what a pick writes, and what a send
// carries.
//
// A send is NOT a mode write. The daemon owns an existing chat's mode, so a
// pill still catching up must never ride along on a prompt and revert the real
// one. The pick is the only writer, and what it writes has to survive the next
// mount, which reads the chat list before any read comes back.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const USER = { id: "u0", author: "user", status: "done", parts: [{ id: "u0-t", kind: "text", text: "Build it" }] };
const ASSISTANT_DONE = {
  id: "a1",
  author: "assistant",
  status: "done",
  completedAt: "2026-06-10T00:00:01Z",
  parts: [{ id: "a1-t", kind: "text", text: "Done." }],
};

const { ds, state, hostRequest } = vi.hoisted(() => {
  // `chats` is the chat-list snapshot the pill falls back to, so a test can make
  // it lag behind the persisted mode. `modeSeed` holds the authoritative
  // getPermissionMode read open across an interaction.
  const state = {
    turns: [] as unknown[],
    chats: [] as Record<string, unknown>[],
    modeSeed: null as Promise<string> | null,
  };
  const ds = {
    listChats: async () => state.chats,
    listModels: async () => [
      { id: "model-alpha", displayName: "Model Alpha", efforts: ["low", "high"], defaultEffort: "low" },
    ],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: async () => [] as unknown[],
    listContext: async () => ({ total: 0, items: [] as unknown[] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] as unknown[] }),
    getChatTurns: vi.fn(async () => state.turns),
    searchFiles: async () => [] as unknown[],
    subscribeChat: () => () => {},
    subscribePermissionMode: () => () => {},
    getPermissionMode: () => state.modeSeed ?? Promise.resolve("default"),
    setPermissionMode: vi.fn(async () => {}),
    setEffort: vi.fn(async () => {}),
    createChat: vi.fn(),
    sendUserMessage: vi.fn(async (_chatId: string, _text: string, _opts?: Record<string, unknown>) => ({})),
  };
  const hostRequest = vi.fn(async () => ({}));
  return { ds, state, hostRequest };
});

vi.mock("./data", () => ({
  chatHost: () => ({
    engine: { request: hostRequest },
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
import { MODE_OPTIONS } from "./options";

// The labels come from the SAME vocabulary the surface renders, so a wording
// change moves test and source together.
const modeLabel = (value: string): string => MODE_OPTIONS.find((mode) => mode.value === value)?.label ?? value;

/** A promise a test resolves by hand, so an async read stays open across an
 *  interaction (the mode seed racing the first send). */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/** The mode chip. Its accessible name carries the current value at every width,
 *  so it is both the control and the readout. */
const modePill = (): HTMLElement => screen.getByRole("button", { name: /^permission mode:/i });

async function pickMode(value: string): Promise<void> {
  await act(async () => {
    fireEvent.click(modePill());
  });
  // The mode menu also carries folded model and effort sections; its own
  // options live in the listbox named for the mode itself.
  const list = screen.getByRole("listbox", { name: /^permission mode$/i });
  await act(async () => {
    fireEvent.click(within(list).getByRole("option", { name: new RegExp(`^${modeLabel(value)}\\b`) }));
  });
}

async function typeAndSend(text: string): Promise<void> {
  const box = await screen.findByRole("textbox", { name: /message/i });
  await act(async () => {
    fireEvent.change(box, { target: { value: text } });
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
  });
}

/** The options the surface handed the data source on its last send. */
function lastSendOptions(): Record<string, unknown> | undefined {
  return ds.sendUserMessage.mock.calls.at(-1)?.[2];
}

/** An existing chat whose durable copies lag the live session. */
function seedExistingChat(permissionMode = "default"): void {
  state.turns = [USER, ASSISTANT_DONE];
  state.chats = [
    {
      id: "c1",
      title: "Chat",
      updatedAt: "2026-06-10T00:00:00Z",
      permissionMode,
      model: { id: "model-alpha", efforts: ["low", "high"], effort: "low" },
    },
  ];
}

function renderChat(client?: QueryClient) {
  const qc = client ?? createQueryClient({ retry: false });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:id" element={<ChatSurface />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, qc };
}

const composerShown = (): Promise<HTMLElement> => screen.findByRole("textbox", { name: /message/i });

afterEach(() => {
  cleanup();
  state.turns = [];
  state.chats = [];
  state.modeSeed = null;
  vi.clearAllMocks();
  ds.setPermissionMode.mockResolvedValue(undefined);
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

describe("ChatSurface permission mode", () => {
  it("shows the chat's persisted mode without waiting for the daemon read", async () => {
    // A fresh mount paints before the authoritative read resolves; the pill must
    // already say what the chat persisted, never a "default" placeholder.
    seedExistingChat("auto");
    state.modeSeed = new Promise<string>(() => {});
    renderChat();

    await composerShown();
    expect(modePill()).toHaveAccessibleName(`Permission mode: ${modeLabel("auto")}`);
  });

  it("applies a mode pick to the live session at once", async () => {
    seedExistingChat();
    renderChat();
    await composerShown();

    await pickMode("auto");

    // The host pushes it to the running session NOW, not only on the next send.
    await waitFor(() => expect(ds.setPermissionMode).toHaveBeenCalledWith("c1", "auto"));
    expect(modePill()).toHaveAccessibleName(`Permission mode: ${modeLabel("auto")}`);
  });

  it("warns that the mode may be stale when the daemon read fails", async () => {
    // A failed read leaves the pill on the chat list's value, which can be older
    // than what the agent is running under. Say so rather than let the user
    // trust a number nobody checked.
    const refused = Promise.reject({ message: "daemon offline" });
    refused.catch(() => {}); // the surface is the real handler; this only keeps Node quiet
    state.modeSeed = refused;
    seedExistingChat();
    renderChat();

    expect(await screen.findByText(/the mode shown may be stale/i)).toBeInTheDocument();
    expect(screen.getByText(/daemon offline/i)).toBeInTheDocument();
  });

  it("surfaces a dismissable banner when applying the mode fails", async () => {
    // A live switch the daemon rejects must tell the user WHY, not silently
    // no-op, and let them put the banner away.
    ds.setPermissionMode.mockRejectedValue({ message: "daemon offline" });
    seedExistingChat();
    renderChat();
    await composerShown();

    await pickMode("auto");

    // Titled by what did not happen, with the reason under it; never "Command failed".
    expect(await screen.findByText("Couldn't switch the mode")).toBeInTheDocument();
    expect(screen.getByText("daemon offline")).toBeInTheDocument();
    expect(screen.queryByText(/command failed/i)).not.toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^dismiss$/i }));
    });
    expect(screen.queryByText("Couldn't switch the mode")).not.toBeInTheDocument();
  });

  it("sends the model and the effort and never the mode", async () => {
    // The chat list still says "default", the authoritative
    // read hasn't landed, and the user sends. The prompt must carry no mode at
    // all, so the chat's persisted "auto" survives.
    const seed = deferred<string>();
    state.modeSeed = seed.promise;
    seedExistingChat("default");
    renderChat();
    await composerShown();
    expect(modePill()).toHaveAccessibleName(`Permission mode: ${modeLabel("default")}`);

    await typeAndSend("hi");

    await waitFor(() => expect(ds.sendUserMessage).toHaveBeenCalledWith("c1", "hi", expect.anything()));
    expect(lastSendOptions()).toMatchObject({ model: { id: "model-alpha" }, effort: "low" });
    expect(lastSendOptions()).not.toHaveProperty("mode");
    expect(ds.setPermissionMode).not.toHaveBeenCalled();

    // The read lands after the send; the pill follows it.
    await act(async () => {
      seed.resolve("auto");
      await seed.promise;
    });
    await waitFor(() => expect(modePill()).toHaveAccessibleName(`Permission mode: ${modeLabel("auto")}`));
  });

  it("keeps an explicit mode pick out of the next send", async () => {
    seedExistingChat();
    renderChat();
    await composerShown();

    await pickMode("plan");
    await waitFor(() => expect(ds.setPermissionMode).toHaveBeenCalledWith("c1", "plan"));

    // The pick is the ONLY writer. The send that follows rides on it without
    // re-asserting it.
    await typeAndSend("hi");
    await waitFor(() => expect(ds.sendUserMessage).toHaveBeenCalled());
    expect(lastSendOptions()).not.toHaveProperty("mode");
    expect(ds.setPermissionMode).toHaveBeenCalledTimes(1);
  });

  it("repaints an accepted mode pick on the next mount", async () => {
    // The chat list the daemon serves still lags, so only the write-back into
    // the cached list can paint the pick on a mount whose read never lands.
    seedExistingChat("default");
    const first = renderChat();
    await composerShown();
    await pickMode("auto");
    await waitFor(() => expect(ds.setPermissionMode).toHaveBeenCalledWith("c1", "auto"));

    first.unmount();
    // Drop the webview-lifetime copy of the pick, and hold the authoritative
    // read open: the cached chat list is the only source left.
    useChatStore.setState({ byId: {}, composerPrefs: {} });
    state.modeSeed = new Promise<string>(() => {});
    renderChat(first.qc);

    await composerShown();
    expect(modePill()).toHaveAccessibleName(`Permission mode: ${modeLabel("auto")}`);
  });
});

// The composer's picks survive navigation. Leaving a chat unmounts the whole
// surface, so mode/model/effort live in the chat store's per-chat prefs slice —
// the daemon's durable copies (manifest mode, pinned model/effort) lag behind a
// fresh pick, and a draft chat has no durable copy at all. On return the first
// paint must show what the user chose, not the stale fallback. When the draft
// becomes a real chat, `claimDraftPrefs` carries the picks across.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const USER = { id: "u0", author: "user", status: "done", parts: [{ id: "u0-t", kind: "text", text: "Build it" }] };
const ASSISTANT_DONE = {
  id: "a1",
  author: "assistant",
  status: "done",
  completedAt: "2026-06-10T00:00:01Z",
  parts: [{ id: "a1-t", kind: "text", text: "Done." }],
};

const { ds, state, hostRequest } = vi.hoisted(() => {
  // `chats` carries the daemon's STALE durable copies (permissionMode, pinned
  // model/effort) so a remount can only show a fresh pick via the store slice.
  // `modeSeed` holds the authoritative getPermissionMode read open on remount.
  const state = {
    turns: [] as unknown[],
    chats: [] as Record<string, unknown>[],
    modeSeed: null as Promise<string> | null,
  };
  const ds = {
    listChats: async () => state.chats,
    listModels: async () => [
      { id: "model-alpha", displayName: "Model Alpha", efforts: ["low", "high"], defaultEffort: "low" },
      { id: "model-beta", displayName: "Model Beta", efforts: ["low", "high"], defaultEffort: "low" },
    ],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: async () => [] as unknown[],
    getChatTurns: vi.fn(async () => state.turns),
    searchFiles: async () => [] as unknown[],
    subscribeChat: () => () => {},
    subscribePermissionMode: () => () => {},
    getPermissionMode: () => state.modeSeed ?? Promise.resolve("default"),
    setPermissionMode: vi.fn(async () => {}),
    setEffort: vi.fn(async () => {}),
    createChat: vi.fn(async () => ({ id: "c1", title: "Build it", updatedAt: "2026-06-10T00:00:00Z" })),
    sendUserMessage: vi.fn(async () => ({})),
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
    // The chrome's useAuthState consumes the full auth channel; a stub with no
    // cache and no pushes keeps it on its INITIAL (signed-out) state.
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
  errorText: (err: unknown) =>
    typeof err === "object" && err !== null && typeof (err as { message?: unknown }).message === "string"
      ? (err as { message: string }).message
      : String(err),
  exportBlob: async () => {},
}));

import { ChatSurface } from "./ChatSurface";
import { DRAFT_CHAT_KEY, useChatStore } from "./chatStore";
import { MODE_OPTIONS, effortOptions } from "./options";

// The option labels come from the SAME mappings the surface renders, so a
// vocabulary change moves test and source together.
const modeLabel = (value: string): string => MODE_OPTIONS.find((mode) => mode.value === value)?.label ?? value;
const effortLabel = (value: string): string =>
  effortOptions(["low", "high"]).find((effort) => effort.value === value)?.label ?? value;

type Rail = "model" | "effort" | "mode";

// The composer names each chip through its `data-rail` attribute (the rail's
// own lookup contract), so the chips are addressed by rail, not by their
// display strings.
function ctl(rail: Rail): HTMLElement {
  const el = document.querySelector<HTMLElement>(`.chat-composer-ctl[data-rail="${rail}"]`);
  if (!el) throw new Error(`the rail has no ${rail} control`);
  return el;
}

/** The chip's visible value ("Auto", "Model Beta"). The trigger is the rail's
 *  only role=button — its menu options carry role=option. */
const railValue = (rail: Rail): HTMLElement => within(ctl(rail)).getByRole("button");

async function pick(rail: Rail, optionLabel: string): Promise<void> {
  await act(async () => {
    fireEvent.click(railValue(rail));
  });
  // The rail's own options lead its pop; the mode menu also carries folded
  // model/effort sections after them. A row's accessible name appends the
  // option's description, so match on the leading label.
  const list = within(ctl(rail)).getAllByRole("listbox")[0];
  await act(async () => {
    fireEvent.click(within(list).getByRole("option", { name: new RegExp(`^${optionLabel}\\b`) }));
  });
}

afterEach(() => {
  cleanup();
  state.turns = [];
  state.chats = [];
  state.modeSeed = null;
  vi.clearAllMocks();
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

function renderAt(entry: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/chat" element={<ChatSurface />} />
          <Route path="/chat/:id" element={<ChatSurface />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const composerShown = async (): Promise<void> => {
  // The model chip renders only once the catalog lands — it is the last rail
  // to appear, so waiting on it settles the whole rail.
  await waitFor(() => expect(document.querySelector('.chat-composer-ctl[data-rail="model"]')).not.toBeNull());
};

/** An EXISTING chat whose durable copies are stale relative to the fresh picks. */
function seedExistingChat(): void {
  state.turns = [USER, ASSISTANT_DONE];
  state.chats = [
    {
      id: "c1",
      title: "Chat",
      updatedAt: "2026-06-10T00:00:00Z",
      permissionMode: "default",
      model: { id: "model-alpha", efforts: ["low", "high"], effort: "low" },
    },
  ];
}

describe("ChatSurface prefs across leave-and-return (existing chat)", () => {
  it("repaints a picked mode and effort from the store, not the daemon", async () => {
    seedExistingChat();
    const first = renderAt("/chat/c1");
    await composerShown();

    await pick("mode", modeLabel("auto"));
    await waitFor(() => expect(ds.setPermissionMode).toHaveBeenCalledWith("c1", "auto"));
    await pick("effort", effortLabel("high"));
    await waitFor(() => expect(ds.setEffort).toHaveBeenCalledWith("c1", "high"));

    // Leave the chat (full unmount), then return. The chat list still carries
    // the STALE durable copies (mode "default", pinned effort "low") and the
    // authoritative mode read never lands — the store slice is the only source
    // that can paint the picks.
    first.unmount();
    state.modeSeed = new Promise<string>(() => {});
    renderAt("/chat/c1");
    await composerShown();

    await waitFor(() => expect(railValue("mode")).toHaveTextContent(modeLabel("auto")));
    expect(railValue("effort")).toHaveTextContent(effortLabel("high"));
    expect(railValue("mode")).not.toHaveTextContent(modeLabel("default"));
    expect(railValue("effort")).not.toHaveTextContent(effortLabel("low"));
  });
});

describe("ChatSurface prefs on a draft chat", () => {
  it("keeps a draft's model and effort across leave-and-return", async () => {
    const first = renderAt("/chat");
    await composerShown();
    // The draft seeds from the catalog: first model, its default effort.
    expect(railValue("model")).toHaveTextContent("Model Alpha");

    await pick("model", "Model Beta");
    await waitFor(() => expect(railValue("model")).toHaveTextContent("Model Beta"));
    const afterModelChange = useChatStore.getState().composerPrefs[DRAFT_CHAT_KEY];
    expect(afterModelChange).toMatchObject({ model: "model-beta" });
    expect(afterModelChange?.effort).toBeUndefined();

    await pick("effort", effortLabel("high"));
    expect(useChatStore.getState().composerPrefs[DRAFT_CHAT_KEY]).toMatchObject({
      model: "model-beta",
      effort: "high",
    });

    // A draft has no chat to persist to — the pick must NOT hit the daemon.
    expect(ds.setEffort).not.toHaveBeenCalled();
    expect(ds.setPermissionMode).not.toHaveBeenCalled();

    first.unmount();
    renderAt("/chat");
    await composerShown();

    // First paint shows the picks, not the catalog fallback.
    await waitFor(() => expect(railValue("model")).toHaveTextContent("Model Beta"));
    expect(railValue("effort")).toHaveTextContent(effortLabel("high"));
  });

  it("transfers the draft's picks to the created chat and resets it", async () => {
    const first = renderAt("/chat");
    await composerShown();
    await pick("model", "Model Beta");
    await pick("effort", effortLabel("high"));

    // Send the first message: createChat resolves a chat WITHOUT a pinned
    // model, so only the claimed draft prefs can label the composer on /chat/c1.
    fireEvent.change(screen.getByRole("textbox", { name: /message/i }), { target: { value: "Build it" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
    });
    await waitFor(() => expect(ds.createChat).toHaveBeenCalled());

    // Navigated into the real chat, the picks carried over…
    await waitFor(() => expect(railValue("model")).toHaveTextContent("Model Beta"));
    expect(railValue("effort")).toHaveTextContent(effortLabel("high"));
    // …and landed under the real id, with the draft key reset for the next chat.
    const prefs = useChatStore.getState().composerPrefs;
    expect(prefs.c1).toMatchObject({ model: "model-beta", effort: "high" });
    expect(DRAFT_CHAT_KEY in prefs).toBe(false);

    first.unmount();
  });
});

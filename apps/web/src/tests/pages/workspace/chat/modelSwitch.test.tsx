// Switching the model on an OPEN chat, in a browser tab.
//
// The defect this file exists for: the picker was local component state. A
// reader picked Haiku, the turn really did run on Haiku, and on the way back
// the chip read the workspace default over a transcript another model wrote —
// with the next turn quietly billed at the default's rate.
//
// So the test drives the REAL chat surface over the browser's own routes and
// asserts the things that make a pick durable: the source is asked to persist
// it, the chat list is re-read afterwards (that cached list is where the chip
// reads the pin on its next mount), the chip then states what came back, and a
// refusal is surfaced rather than left as a chip pointing at a model the chat
// is not on. The client is `createQueryClient`, the one the app builds, so the
// cache behaves the way it does in the product.
//
// This file states the capability itself and serves no per-model verdicts, so
// what it proves is "where a source CAN move an open chat, the pick is
// persisted, re-read and surfaced". The verdicts (a model the chat's reasoning
// rules out, the way out through a new chat) are `openChatModelPicker.test.tsx`.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const CATALOG = [
  {
    id: "claude-fable-5",
    displayName: "Claude Fable 5",
    wire: "anthropic",
    efforts: ["low", "medium", "high"],
    defaultEffort: "medium",
  },
  {
    id: "claude-haiku-4.5",
    displayName: "Claude Haiku 4.5",
    wire: "anthropic",
    efforts: [],
    defaultEffort: null,
  },
];

const { ds, state } = vi.hoisted(() => {
  const state = {
    /** What the chat row says its model is — the server's answer, re-read on
     *  every `listChats`, so a stale local guess cannot satisfy the assertions. */
    pinned: null as { id: string; efforts: string[]; effort: string | null } | null,
    listChatCalls: 0,
  };
  return { state, ds: {} as Record<string, unknown> };
});

const setModel = vi.fn(async (_chatId: string, model: string) => {
  state.pinned = { id: model, efforts: [], effort: null };
});

Object.assign(ds, {
  listChats: async () => {
    state.listChatCalls += 1;
    return [
      {
        id: "c1",
        title: "Ops",
        updatedAt: "2026-09-06T12:00:00Z",
        permissionMode: "read_only",
        ...(state.pinned ? { model: state.pinned } : {}),
      },
    ];
  },
  listModels: async () => CATALOG,
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
  setEffort: async () => {},
  setModel,
  subscribePermissionMode: () => () => {},
  subscribeChat: () => () => {},
  getChatActivity: () => ({}),
  getSubagentChats: () => [],
  getSubagentLabels: () => ({}),
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const host = real.createBrowserChatHost({
    account: () => ({ email: "analyst@tideline.example", webAppUrl: null }),
  });
  return {
    ...real,
    chatHost: () => host,
    chatData: () => ds,
    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
    // The cloud source's real capabilities, including the one this change adds:
    // it serves a catalogue AND can move an open chat onto another model.
    chatCaps: () => ({
      opencodeActive: false,
      modelCatalog: true,
      switchableModel: true,
      permissionModes: ["read_only", "default", "plan"],
      fixedPermissionMode: "read_only",
    }),
  };
});

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";

beforeEach(() => {
  state.pinned = null;
  state.listChatCalls = 0;
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
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function renderChat() {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatSurface chatId="c1" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The composer's model chip. Its accessible name carries the CURRENT model,
 *  which is what the reader sees and therefore what the test reads. */
async function modelChip(): Promise<HTMLButtonElement> {
  return (await screen.findByRole("button", { name: /^Model: / })) as HTMLButtonElement;
}

/** Open the chip and pick `label` out of its menu. Scoped to the menu, because
 *  the chip itself also spells the model it is currently on. */
async function pickModel(label: string): Promise<void> {
  fireEvent.click(await modelChip());
  const menu = await screen.findByRole("listbox", { name: "Model" });
  fireEvent.click(within(menu).getByText(label));
}

describe("the model picked on an open chat", () => {
  it("is offered at all — the picker is not greyed on a source that can move it", async () => {
    renderChat();

    const chip = await modelChip();

    // The regression guard for the other half of the defect: a greyed chip
    // means the reader cannot switch at all, and every assertion below is
    // unreachable from the UI even with the route in place.
    expect(chip.disabled).toBe(false);
    fireEvent.click(chip);
    const menu = await screen.findByRole("listbox", { name: "Model" });
    expect(within(menu).getByText("Claude Haiku 4.5")).toBeTruthy();
  });

  it("is persisted through the source, not kept in component state", async () => {
    renderChat();

    await pickModel("Claude Haiku 4.5");

    await waitFor(() => expect(setModel).toHaveBeenCalledTimes(1));
    // The chat id rides the call — the row is the model's home, so a switch
    // that named no chat would have nowhere to land — and so does the model it
    // was switched FROM (none: this chat was never pinned), so a chat somebody
    // else moved meanwhile is refused.
    expect(setModel).toHaveBeenCalledWith("c1", "claude-haiku-4.5", undefined, null);
  });

  it("re-reads the chat list, so the chip's next mount reads the server's pin", async () => {
    renderChat();
    await modelChip();
    await waitFor(() => expect(state.listChatCalls).toBeGreaterThan(0));
    const before = state.listChatCalls;

    await pickModel("Claude Haiku 4.5");

    // Asserted through a REAL refetch of the source, not an invalidate spy: a
    // switch the chip forgot to refresh leaves the next mount reading a stale
    // list, which is exactly how the old model came back after navigation.
    await waitFor(() => expect(state.listChatCalls).toBeGreaterThan(before));
    expect(state.pinned?.id).toBe("claude-haiku-4.5");
  });

  it("shows the switched model after the list comes back, not the default", async () => {
    renderChat();

    await pickModel("Claude Haiku 4.5");

    await waitFor(async () =>
      expect((await modelChip()).getAttribute("aria-label")).toBe("Model: Claude Haiku 4.5"),
    );
  });

  it("says why a refused switch did not happen, never as a failed command", async () => {
    setModel.mockRejectedValueOnce(
      new Error("claude-haiku-4.5 isn't a model this chat's owner can run"),
    );
    renderChat();

    await pickModel("Claude Haiku 4.5");

    expect(await screen.findByText("Couldn't switch the model")).toBeTruthy();
    expect(screen.getByText("claude-haiku-4.5 isn't a model this chat's owner can run")).toBeTruthy();
    expect(screen.queryByText("Command failed")).toBeNull();
  });
});

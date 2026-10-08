// The empty composer's first Send carries all three picks — model, effort,
// stance — into the chat it starts.
//
// Home has no chat yet, so Send hands the message and the picks to the chat
// surface (the router state `startNewChat` writes), which creates the chat with
// them; `createChatWire.test.ts` pins that the create body then names
// `model`, `effort` and `permission_mode` on the wire. What is pinned here is
// the hop before it: the picks the chip and the pickers show are exactly what
// the handoff carries, so a chat never opens on a model, effort or stance the
// composer did not promise.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { ModelInfo } from "@/pages/workspace/chat/data/model";

const { ds, catalog } = vi.hoisted(() => {
  const catalog: {
    models: unknown[];
    seed: Record<string, string | null>;
    /** Held open by a test that needs to send BEFORE the seed answers. */
    held: Promise<void> | null;
  } = {
    models: [],
    seed: { model: null, effort: null, permissionMode: "read_only" },
    held: null,
  };
  const ds = {
    listChats: async () => [],
    listModels: async () => catalog.models,
    resolveChatDefaults: async () => {
      if (catalog.held) await catalog.held;
      return catalog.seed;
    },
    listCommands: async () => [],
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: async () => [],
    searchFiles: async () => [],
    subscribeChat: () => () => {},
    subscribePermissionMode: () => () => {},
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
    createChat: vi.fn(),
    sendUserMessage: vi.fn(),
  };
  return { ds, catalog };
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const host = real.createBrowserChatHost({
    account: () => ({ email: "analyst@tideline.example", webAppUrl: null }),
  });
  const { PERMISSION_MODE_VALUES: cloudModes } = await import("@alkera/chat-model");
  return {
    ...real,
    chatHost: () => host,
    chatData: () => ds,
    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
    chatCaps: () => ({
      opencodeActive: false,
      modelCatalog: true,
      permissionModes: cloudModes,
      fixedPermissionMode: "read_only",
    }),
  };
});

import { ChatHomeSurface } from "@/pages/workspace/chat/ChatHomeSurface";

const OPUS: ModelInfo = {
  id: "claude-opus-4.5",
  displayName: "Claude Opus 4.5",
  wire: "anthropic",
  efforts: ["low", "medium", "high"],
  defaultEffort: "medium",
};

/** Renders the router state the home composer's Send hands the chat surface. */
function Handoff(): ReactElement {
  const location = useLocation();
  const state = location.state as { pendingMessage?: string; pendingOptions?: unknown } | null;
  return <pre data-testid="handoff">{state?.pendingMessage ? JSON.stringify(state) : ""}</pre>;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  catalog.models = [];
  catalog.seed = { model: null, effort: null, permissionMode: "read_only" };
  catalog.held = null;
});

function renderHome(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
    ),
  );
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route
            path="/chat"
            element={
              <>
                <ChatHomeSurface />
                <Handoff />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function send(text: string): Promise<Record<string, unknown>> {
  const box = await screen.findByRole("textbox", { name: /^message /i });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
  await waitFor(() => expect(screen.getByTestId("handoff").textContent).not.toBe(""));
  return JSON.parse(screen.getByTestId("handoff").textContent ?? "{}") as Record<string, unknown>;
}

describe("the home composer's first Send", () => {
  it("hands the chat surface the model, effort and stance the composer showed", async () => {
    catalog.models = [OPUS];
    catalog.seed = { model: OPUS.id, effort: "high", permissionMode: "plan" };
    renderHome();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^permission mode:/i })).toHaveTextContent(/plan/i),
    );

    const handoff = await send("why are prompts down?");

    expect(handoff).toEqual({
      pendingMessage: "why are prompts down?",
      pendingOptions: { model: OPUS, effort: "high", mode: "plan" },
      into: null,
    });
  });

  it("names the effort the reader picked over the seeded one", async () => {
    catalog.models = [OPUS];
    catalog.seed = { model: OPUS.id, effort: "high", permissionMode: "default" };
    renderHome();
    const effort = await screen.findByRole("button", { name: /^effort: high/i });
    fireEvent.click(effort);
    fireEvent.click(await screen.findByRole("option", { name: /^low/i }));

    const handoff = await send("hello");

    expect(handoff.pendingOptions).toEqual({ model: OPUS, effort: "low", mode: "default" });
  });

  it("names no model or effort while the catalog is empty, so the server resolves them", async () => {
    catalog.models = [];
    catalog.seed = { model: null, effort: null, permissionMode: "read_only" };
    renderHome();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^permission mode:/i })).toBeInTheDocument(),
    );

    const handoff = await send("hello");

    expect(handoff.pendingOptions).toEqual({ mode: "read_only" });
  });

  // The stance is the one pick this composer cannot guess at: the server holds
  // the reader's saved default and applies it to a create that names none, so
  // sending the source's floor in that gap overrides the saved stance with a
  // browser-side constant — a chat the reader had set to run in `default` opens
  // read-only, and nothing on screen says so.
  it("names no stance before the seed answers, so the saved default still decides", async () => {
    catalog.models = [];
    catalog.seed = { model: null, effort: null, permissionMode: "default" };
    catalog.held = new Promise(() => {});
    renderHome();

    const handoff = await send("why are prompts down?");

    expect(handoff.pendingOptions).toEqual({});
    expect(screen.queryByRole("button", { name: /^permission mode:/i })).toBeNull();
  });

  it("carries the stance the reader picked over the seeded one", async () => {
    catalog.models = [];
    catalog.seed = { model: null, effort: null, permissionMode: "default" };
    renderHome();
    fireEvent.click(await screen.findByRole("button", { name: /^permission mode:/i }));
    fireEvent.click(await screen.findByRole("option", { name: /^plan/i }));

    const handoff = await send("hello");

    expect(handoff.pendingOptions).toEqual({ mode: "plan" });
  });
});

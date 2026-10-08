// What the home composer's permission chip promises, against what the chat it
// starts actually runs in.
//
// The chip is a safety label: it names the stance the agent will be held to in
// the chat this composer is about to create. A browser-side constant such as
// `read_only` would understate the permissions of a reader whose saved stance
// lets the agent edit files and run commands, which is the direction that
// matters for a safety label.
//
// Both halves are pinned here: the chip reads the
// stance the server resolved for a NEW chat, and a stance the reader picks on
// that chip rides the create instead of being dropped.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PERMISSION_MODE_VALUES } from "@alkera/chat-model";

import { createQueryClient } from "@/api/queryClient";
import type { DocHandle } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import type { PermissionMode } from "@/pages/workspace/chat/data/model";

const { ds, seed } = vi.hoisted(() => {
  // The new-chat seed the server answered for this reader. A test sets the
  // stance before rendering.
  const seed: { permissionMode: string; held: Promise<void> | null } = {
    permissionMode: "read_only",
    held: null,
  };
  const ds = {
    listChats: async () => [],
    listModels: async () => [],
    resolveChatDefaults: async () => {
      if (seed.held) await seed.held;
      return { model: null, effort: null, permissionMode: seed.permissionMode };
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
  return { ds, seed };
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
    // The cloud source's real capabilities, its fixed mode included: that is
    // the fallback shown before the seed lands, and it must not outrank the
    // seed once the seed is there. The stance list is taken from the source
    // itself rather than copied, so a list the product widens cannot leave the
    // home composer's chip behind.
    chatCaps: () => ({
      opencodeActive: false,
      modelCatalog: true,
      permissionModes: cloudModes,
      fixedPermissionMode: "read_only",
    }),
  };
});

import { ChatHomeSurface } from "@/pages/workspace/chat/ChatHomeSurface";
import { MODE_OPTIONS } from "@/pages/workspace/chat/options";

// The labels come from the SAME vocabulary the surface renders, so a wording
// change moves test and source together.
const modeLabel = (value: string): string =>
  MODE_OPTIONS.find((mode) => mode.value === value)?.label ?? value;

const cloudModeSet = new Set<string>(PERMISSION_MODE_VALUES);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  seed.permissionMode = "read_only";
  seed.held = null;
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
        <ChatHomeSurface />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the home composer's permission chip", () => {
  it.each([["plan"], ["default"], ["read_only"], ["bypass"]])(
    "states the stance the server resolved for the next chat (%s)",
    async (mode) => {
      seed.permissionMode = mode;
      renderHome();

      await waitFor(() => {
        expect(screen.getByRole("button", { name: /^permission mode:/i })).toHaveTextContent(
          modeLabel(mode),
        );
      });
    },
  );

  // A safety label may not fill its own gap: the source's floor is a guess at
  // an answer only the server has, so while that answer is in flight the chip
  // names no stance at all rather than one the create will not ask for.
  it("promises no stance while the server's answer is still in flight", async () => {
    seed.held = new Promise(() => {});
    renderHome();
    // The composer itself is up — the gap is the chip's, not the send's.
    await screen.findByRole("textbox", { name: /^message /i });

    expect(screen.queryByRole("button", { name: /^permission mode:/i })).toBeNull();
  });

  it("states the stance the moment the answer lands", async () => {
    let answer = (): void => {};
    seed.permissionMode = "default";
    seed.held = new Promise<void>((resolve) => {
      answer = resolve;
    });
    renderHome();
    await screen.findByRole("textbox", { name: /^message /i });
    expect(screen.queryByRole("button", { name: /^permission mode:/i })).toBeNull();

    answer();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^permission mode:/i })).toHaveTextContent(
        modeLabel("default"),
      );
    });
  });

  // The home composer is the FIRST place a reader meets the control, before
  // there is a chat to switch. Offering fewer stances here than the chat page
  // does would make the stance a thing you can only reach after starting in
  // one you did not want.
  it("offers every stance the chat page does, in the same order", async () => {
    renderHome();
    const chip = await screen.findByRole("button", { name: /^permission mode:/i });
    fireEvent.click(chip);

    const expected = MODE_OPTIONS.filter((mode) => cloudModeSet.has(mode.value)).map(
      (mode) => mode.label,
    );
    const labels = screen
      .getAllByRole("option")
      .map((option) => expected.find((name) => (option.textContent ?? "").startsWith(name)));
    expect(labels).toEqual(expected);
    expect(expected).toContain("Bypass permissions");
  });
});

// --- the other half: a picked stance has to reach the create -----------------

function cloudSource() {
  const created: Record<string, unknown>[] = [];
  const rest = {
    createChat: vi.fn(async (title: string | null, body: Record<string, unknown>) => {
      created.push(body);
      return { id: "chat-new", title, machine_status: "ready", machine_id: null };
    }),
    postMessage: vi.fn(async (_chatId: string, body: Record<string, unknown>) => ({
      id: "m1",
      seq: 1,
      kind: "user_message",
      text: String(body.text),
      created_at: "",
    })),
    chatDefaults: vi.fn(async () => ({ model: "m-1", effort: "high", permission_mode: "plan" })),
  };
  const source = new CloudDataSource({
    rest: rest as never,
    openDoc: () => ({ subscribe: () => () => {}, close: () => {} }) as unknown as DocHandle<never>,
    acquire: () => () => {},
    clientId: () => "client-1",
  });
  return { source, created };
}

describe("CloudDataSource.createChat", () => {
  it("pins the stance the composer showed onto the chat it creates", async () => {
    const { source, created } = cloudSource();

    await source.createChat("churn by segment", { mode: "plan" });

    expect(created[0]).toMatchObject({ permissionMode: "plan" });
  });

  it("names no stance when the composer offered none, so the saved default decides", async () => {
    const { source, created } = cloudSource();

    await source.createChat("churn by segment");

    expect(Object.keys(created[0])).not.toContain("permissionMode");
  });

  it("pins bypass, the stance a reader picks to run a job unattended", async () => {
    const { source, created } = cloudSource();

    await source.createChat("churn by segment", { mode: "bypass" });

    expect(created[0]).toMatchObject({ permissionMode: "bypass" });
  });

  // The guard is still there, it is just no longer a list of five minus two:
  // anything the create route would refuse outright is dropped rather than
  // sent, because the whole create is what a 422 costs.
  it("drops a stance this source cannot open a chat in rather than losing the create", async () => {
    const { source, created } = cloudSource();

    // Cast: the point is exactly what happens when a mode the types do not
    // admit reaches the source — a newer composer, a resumed draft, an older
    // build's saved default.
    await source.createChat("churn by segment", { mode: "accept_edits" as PermissionMode });

    expect(Object.keys(created[0])).not.toContain("permissionMode");
  });

  it("carries the server's resolved stance into the new-chat seed", async () => {
    const { source } = cloudSource();

    expect(await source.resolveChatDefaults()).toEqual({
      model: "m-1",
      effort: "high",
      permissionMode: "plan",
    });
  });
});

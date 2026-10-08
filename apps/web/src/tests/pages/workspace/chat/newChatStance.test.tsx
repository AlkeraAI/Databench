// What stance the portal pins on a chat it is about to create, and what its
// chip says while it decides.
//
// The portal's `/chat` is the REAL chat surface with no chat yet, so the
// composer's mode chip and the create body come from the same value
// (`useComposerPrefs`' `composerMode`, handed to `submitMessage` by
// `ChatSurface.send`). When the reader picked nothing, the source's own floor
// (`fixedPermissionMode`, "read_only" on the cloud) must not ride
// `POST /api/v1/chats` as `permission_mode`; the server applies their saved
// default.
//
// Driven through the real Composer: the send is a click on Send, and what is
// asserted is the create the source was handed.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const { ds, seed } = vi.hoisted(() => {
  // The new-chat seed `GET /api/v1/me/chat-defaults` answers, and a latch a
  // test holds closed to send while that answer is still in flight.
  const seed: { permissionMode: string | null; held: Promise<void> | null } = {
    permissionMode: "default",
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
    sendUserMessage: vi.fn(async () => ({ id: "m1", role: "user", content: "x" })),
    createChat: vi.fn(async (_text: string, _opts?: Record<string, unknown>) => ({
      id: "c-new",
      title: "Run ls",
      updatedAt: "",
    })),
    getPermissionMode: async () => "default",
    setPermissionMode: async () => {},
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
  };
  return { ds, seed };
});

// The browser's real host and the cloud source's real capabilities — its floor
// included, since the floor is what must not leak onto the create.
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

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";
import { useChatStore } from "@/pages/workspace/chat/chatStore";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  seed.permissionMode = "default";
  seed.held = null;
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

/** The portal's `/chat`: the chat surface with no chat yet. */
function renderNewChat(): void {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat" element={<ChatSurface />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const chip = () => screen.queryByRole("button", { name: /^permission mode:/i });

async function send(text: string): Promise<void> {
  const box = await screen.findByRole("textbox", { name: /^message /i });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
  await waitFor(() => expect(ds.createChat).toHaveBeenCalled());
}

/** The options the surface handed the source's create. */
const createdWith = (): Record<string, unknown> | undefined =>
  ds.createChat.mock.calls.at(-1)?.[1];

describe("the stance a new portal chat is created in", () => {
  it("states the resolved default and creates the chat in it", async () => {
    renderNewChat();
    await waitFor(() => expect(chip()).toHaveTextContent(/default/i));

    await send("run ls in the working directory");

    expect(createdWith()?.mode).toBe("default");
    expect(createdWith()?.mode).not.toBe("read_only");
  });

  it("names no stance while the seed is in flight, so the saved default decides", async () => {
    seed.held = new Promise(() => {});
    renderNewChat();

    await send("run ls in the working directory");

    expect(createdWith()).toEqual({});
    expect(chip()).toBeNull();
  });

  it("never names the source's floor over the reader's resolved default", async () => {
    seed.permissionMode = "plan";
    renderNewChat();
    await waitFor(() => expect(chip()).toHaveTextContent(/plan/i));

    await send("hello");

    expect(createdWith()?.mode).toBe("plan");
  });

  it("carries the stance the reader picked on the chip", async () => {
    renderNewChat();
    fireEvent.click(await screen.findByRole("button", { name: /^permission mode:/i }));
    const list = screen.getByRole("listbox", { name: /^permission mode$/i });
    fireEvent.click(
      Array.from(list.querySelectorAll('[role="option"]')).find((option) =>
        (option.textContent ?? "").startsWith("Bypass permissions"),
      ) as HTMLElement,
    );
    await waitFor(() => expect(chip()).toHaveTextContent(/bypass/i));

    await send("hello");

    expect(createdWith()?.mode).toBe("bypass");
  });
});

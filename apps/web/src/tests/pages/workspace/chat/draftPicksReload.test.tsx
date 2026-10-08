// The picks on the empty composer survive a reload.
//
// A reader who chose Plan on `/chat/new` and reloaded came back to Default,
// with no request made and nothing said: the draft's picks lived only in the
// page's memory. They are now kept in this browser for the signed-in account
// until the chat they were made for is created, which claims them and drops
// the memory. A stance that lets the agent do more than the default never
// comes back on its own.
//
// A reload is the module-level chat store emptied under a fresh mount — the
// state a new page starts from — with the browser's storage left as it was.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { accountKey } from "@alkera/ui/storage";

import { createQueryClient } from "@/api/queryClient";

const { ds, who } = vi.hoisted(() => {
  const who = {
    email: "analyst@tideline.example" as string | null,
    userId: "usr_analyst" as string | null | undefined,
    orgId: "org_a" as string | null | undefined,
  };
  const ds = {
    listChats: async () => [],
    listModels: async () => [],
    resolveChatDefaults: async () => ({ model: null, effort: null, permissionMode: "default" }),
    listCommands: async () => [],
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: async () => [],
    searchFiles: async () => [],
    sendUserMessage: vi.fn(async () => ({ id: "m1", role: "user", content: "x" })),
    createChat: vi.fn(async (_text: string, _opts?: Record<string, unknown>) => ({
      id: "c-new",
      title: "hello",
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
  return { ds, who };
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const host = real.createBrowserChatHost({
    account: () => ({ email: who.email, webAppUrl: null, userId: who.userId, orgId: who.orgId }),
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

/** A fresh page: the store as a new module would hold it. */
function reload(): void {
  cleanup();
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
}

beforeEach(() => {
  window.localStorage.clear();
  who.email = "analyst@tideline.example";
  who.userId = "usr_analyst";
  who.orgId = "org_a";
});

afterEach(() => {
  reload();
  window.localStorage.clear();
  vi.clearAllMocks();
});

function renderNewChat(): void {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/new"]}>
        <Routes>
          <Route path="/chat/new" element={<ChatSurface />} />
          <Route path="/chat/:chatId" element={<div>opened</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const chip = () => screen.queryByRole("button", { name: /^permission mode:/i });

async function pickStance(label: string): Promise<void> {
  fireEvent.click(await screen.findByRole("button", { name: /^permission mode:/i }));
  const list = screen.getByRole("listbox", { name: /^permission mode$/i });
  fireEvent.click(
    Array.from(list.querySelectorAll('[role="option"]')).find((option) =>
      (option.textContent ?? "").startsWith(label),
    ) as HTMLElement,
  );
}

async function send(text: string): Promise<void> {
  const box = await screen.findByRole("textbox", { name: /^message /i });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
  await waitFor(() => expect(ds.createChat).toHaveBeenCalled());
}

describe("a stance picked on the empty composer", () => {
  it("is still picked after a reload, and the chat it starts is created in it", async () => {
    renderNewChat();
    await pickStance("Plan");
    await waitFor(() => expect(chip()).toHaveTextContent(/plan/i));

    reload();
    renderNewChat();
    await waitFor(() => expect(chip()).toHaveTextContent(/plan/i));
    await send("hello");
    expect(ds.createChat.mock.calls.at(-1)?.[1]?.mode).toBe("plan");
  });

  it("is spent by the chat it was made for: the next empty composer starts from the default", async () => {
    renderNewChat();
    await pickStance("Plan");
    await waitFor(() => expect(chip()).toHaveTextContent(/plan/i));
    await send("hello");

    reload();
    renderNewChat();
    await waitFor(() => expect(chip()).toHaveTextContent(/default/i));
  });

  it("belongs to the account that made it", async () => {
    renderNewChat();
    await pickStance("Plan");
    await waitFor(() => expect(chip()).toHaveTextContent(/plan/i));

    reload();
    who.email = "someone-else@tideline.example";
    who.userId = "usr_someone_else";
    renderNewChat();
    await waitFor(() => expect(chip()).toHaveTextContent(/default/i));
  });

  it("belongs to the org it was made in, and is still there on the way back", async () => {
    renderNewChat();
    await pickStance("Plan");
    await waitFor(() => expect(chip()).toHaveTextContent(/plan/i));

    // The same person, switched to another org in this browser.
    reload();
    who.orgId = "org_b";
    renderNewChat();
    await waitFor(() => expect(chip()).toHaveTextContent(/default/i));

    reload();
    who.orgId = "org_a";
    renderNewChat();
    await waitFor(() => expect(chip()).toHaveTextContent(/plan/i));
  });

  it("in a shell that names only the email, is kept under the email with no org", async () => {
    who.userId = undefined;
    who.orgId = undefined;
    renderNewChat();
    await pickStance("Plan");
    await waitFor(() => expect(chip()).toHaveTextContent(/plan/i));
    expect(
      JSON.parse(window.localStorage.getItem(accountKey("analyst@tideline.example", "", "chat.draftPicks")) ?? "null"),
    ).toEqual({ mode: "plan" });

    reload();
    renderNewChat();
    await waitFor(() => expect(chip()).toHaveTextContent(/plan/i));
  });

  it("that lets the agent do more than the default does not come back on its own", async () => {
    renderNewChat();
    await pickStance("Bypass permissions");
    await waitFor(() => expect(chip()).toHaveTextContent(/bypass/i));

    reload();
    renderNewChat();
    await waitFor(() => expect(chip()).toHaveTextContent(/default/i));
    await send("hello");
    expect(ds.createChat.mock.calls.at(-1)?.[1]?.mode).toBe("default");
  });

  it("stored unreadably is ignored, and the composer starts from the default", async () => {
    window.localStorage.setItem(accountKey("usr_analyst", "org_a", "chat.draftPicks"), "{not json");
    renderNewChat();
    await waitFor(() => expect(chip()).toHaveTextContent(/default/i));
  });
});

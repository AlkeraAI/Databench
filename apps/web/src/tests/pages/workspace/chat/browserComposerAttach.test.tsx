// The portal composer offers no way to put a file on a message.
//
// The agent cannot yet read a file attached in the portal, and a control that
// accepts work it cannot carry is worse than no control.
//
// So this file drives the REAL portal chat and pins that neither route in —
// the picker key nor a file dropped on the composer — leaves anything on the
// message. The shared composer keeps its `onAttach` seam for the shell that
// can honour it; what is removed is the portal's wiring to it, and this is the
// file that fails if it comes back before the upload path does.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const { ds } = vi.hoisted(() => ({
  ds: {
    listChats: async () => [{ id: "c1", title: "Chat", updatedAt: "2026-09-06T12:00:00Z" }],
    listModels: async () => [],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: async () => [],
    listContext: async () => ({ total: 0 }),
    lineageRoots: async () => ({}),
    getChatTurns: async () => [],
    searchFiles: async () => [],
    sendUserMessage: vi.fn(async () => ({ id: "m1", role: "user", content: "x" })),
    createChat: vi.fn(async () => ({ id: "c1", title: null, updatedAt: "" })),
    getPermissionMode: async () => "read_only",
    setPermissionMode: async () => {},
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
  },
}));

// The browser's real host, as the /chat route installs it.
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
    chatCaps: () => ({
      opencodeActive: false,
      modelCatalog: true,
      permissionModes: ["read_only", "default", "plan"],
      fixedPermissionMode: "read_only",
    }),
  };
});

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderPortalChat(): void {
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

/** The composer, once the chat has painted. */
async function composer(): Promise<HTMLElement> {
  return await screen.findByRole("textbox", { name: /^message /i });
}

const CSV = new File(["a,b\n1,2\n"], "orders.csv", { type: "text/csv" });

describe("the portal composer and files", () => {
  it("shows no attach key at all", async () => {
    renderPortalChat();
    await composer();
    expect(screen.queryByRole("button", { name: /attach files/i })).toBeNull();
    expect(screen.queryByLabelText(/attach files/i)).toBeNull();
  });

  it("has no file input behind the rail either", async () => {
    renderPortalChat();
    await composer();
    // The door's real control is a hidden `<input type="file">`; a key removed
    // but an input left behind is still a drop target the reader can reach.
    expect(document.querySelector('input[type="file"]')).toBeNull();
  });

  it("takes nothing from a file dropped on the composer", async () => {
    renderPortalChat();
    const box = await composer();
    fireEvent.drop(box, { dataTransfer: { files: [CSV], types: ["Files"] } });
    expect(screen.queryByText("orders.csv")).toBeNull();
  });

  it("takes nothing from a file pasted into it", async () => {
    renderPortalChat();
    const box = await composer();
    fireEvent.paste(box, {
      clipboardData: { files: [CSV], items: [], types: ["Files"], getData: () => "" },
    });
    expect(screen.queryByText("orders.csv")).toBeNull();
  });

  it("still sends the message the reader typed", async () => {
    renderPortalChat();
    const box = await composer();
    fireEvent.change(box, { target: { value: "how many orders last week?" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await vi.waitFor(() => expect(ds.sendUserMessage).toHaveBeenCalled());
    // The send carries the text and no attachment argument behind it.
    const [, text, options] = ds.sendUserMessage.mock.calls[0] as unknown as [
      string,
      string,
      { attachments?: unknown } | undefined,
    ];
    expect(text).toBe("how many orders last week?");
    expect(options?.attachments).toBeUndefined();
  });
});

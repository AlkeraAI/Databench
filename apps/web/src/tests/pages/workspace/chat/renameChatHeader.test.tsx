// Renaming the open chat by its own title in the header.
//
// The rail lists names; it does not edit them. The one way to retitle a chat in
// a browser tab is to press its name at the top of the transcript, which is
// what this file pins: that the rail carries no rename control at all, that the
// header's editor writes through the OBJECT route with the version it read, and
// that everything reading the chat's title — the rail row included — follows
// the write without anyone wiring a refetch.
//
// The chrome itself (the field, Enter/Escape/blur, the long-title case) is
// pinned in @alkera/ui beside the component. What is here is the wiring: the
// page's seam reaching the real chrome, and the request it produces.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

// Only the transcript is stood down. The header under test is the REAL chrome
// the surface mounts — the page's `onRenameTitle` reaches it through the same
// prop the surface hands on — so a seam that stopped being passed through shows
// up here as a title that is no longer pressable.
vi.mock("@/pages/workspace/chat/ChatSurface", async () => {
  const { useChat } = await import("@/api/chats");
  const { ChatChrome } = await import("@/pages/workspace/chat/ChatChrome");
  return {
    ChatSurface: ({
      chatId,
      onRenameTitle,
    }: {
      chatId?: string;
      onRenameTitle?: (title: string) => Promise<void>;
    }) => {
      const chat = useChat(chatId);
      return (
        <div className="chat-root">
          <ChatChrome
            title={chat.data?.title ?? "New chat"}
            onRenameTitle={chatId ? onRenameTitle : undefined}
          />
        </div>
      );
    },
  };
});

import { ChatPage } from "@/pages/workspace/chat/ChatPage";

const CHAT = {
  id: "c1",
  title: "Yesterday's orders",
  machine_id: "m1",
  machine_status: "ready" as const,
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  last_seq: 3,
  files_node_id: "nd_chat",
};

interface Call {
  readonly method: string;
  readonly path: string;
  readonly body: Record<string, unknown> | null;
}

let calls: Call[];
/** The title the stub server holds — a landed PUT moves it, the way the real
 *  one does, so the refetch the mutation policy fires answers the new title
 *  rather than replaying the old one over the optimistic patch. */
let stored: string;

function script({
  canWrite = true,
  putStatus = 200,
}: { canWrite?: boolean; putStatus?: number } = {}): void {
  calls = [];
  stored = "Yesterday's orders";
  const json = (payload: unknown, code = 200): Response =>
    new Response(JSON.stringify(payload), {
      status: code,
      headers: { "content-type": "application/json" },
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      const method = (input instanceof Request ? input.method : init?.method) ?? "GET";
      const raw = init?.body;
      calls.push({
        method,
        path: at,
        body: raw ? (JSON.parse(String(raw)) as Record<string, unknown>) : null,
      });
      if (at.startsWith("/api/v1/objects/")) {
        if (method === "PUT") {
          if (putStatus !== 200) {
            return json(
              { detail: { code: "version_conflict", message: "This object moved on" } },
              putStatus,
            );
          }
          stored = String((raw ? JSON.parse(String(raw)) : {}).title ?? stored);
        }
        return json({ id: "c1", type: "chat", title: stored, version: 4, spec: {} });
      }
      if (/\/api\/v1\/chats\/[^/?]+$/.test(at)) return json({ ...CHAT, title: stored });
      if (at.startsWith("/api/v1/files/drives") && at.includes("/items/")) {
        return json({
          id: "nd_chat",
          driveId: "drv_1",
          kind: "folder",
          name: "c1.alkerachat",
          nameDisplay: stored,
          parentId: "nd_root",
          etag: "1",
          ctag: "c1",
          capabilities: { can_read: true, can_write: canWrite, can_share: false, refusals: {} },
        });
      }
      if (at === "/api/v1/files/drives") {
        return json({ id: "drv_1", orgId: "org_1", rootId: "nd_root", quotaBytes: 1 });
      }
      if (at.startsWith("/api/v1/chats"))
        return json({ items: [{ ...CHAT, title: stored }], next_cursor: null });
      if (url.includes("/api/v1/machines/current")) {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      if (url.includes("/api/v1/auth/me")) return json({ id: "u1", email: "dana@example.com" });
      return json({});
    }),
  );
}

function renderChat(): void {
  const client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const put = (): Call | undefined => calls.find((call) => call.method === "PUT");

const openTitle = async (title: string): Promise<void> => {
  fireEvent.click(await screen.findByRole("button", { name: `Rename chat: ${title}` }));
};
const field = (): HTMLInputElement =>
  screen.getByRole("textbox", { name: "Chat title" }) as HTMLInputElement;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("the rail after the header took the rename", () => {
  it("offers no rename control of its own, by key or by double-click", async () => {
    script();
    renderChat();

    // The row is on screen and the header's own control has resolved, so the
    // page has settled: anything the rail were still offering would be here.
    await openTitle("Yesterday's orders");
    fireEvent.keyDown(field(), { key: "Escape" });

    expect(screen.queryByRole("button", { name: "Rename this chat" })).not.toBeInTheDocument();
    const row = screen.getByRole("button", { name: /Yesterday's orders/, current: "page" });
    fireEvent.doubleClick(row);
    expect(screen.queryByRole("textbox", { name: "Chat title" })).not.toBeInTheDocument();
  });
});

describe("renaming the open chat from its title", () => {
  it("Enter writes the new title through the object route, naming the version it read", async () => {
    script();
    renderChat();

    await openTitle("Yesterday's orders");
    // The field opens on the title that is on screen, not on the node's
    // `c1.alkerachat` filesystem name.
    expect(field().value).toBe("Yesterday's orders");

    fireEvent.change(field(), { target: { value: "Q3 orders" } });
    fireEvent.keyDown(field(), { key: "Enter" });

    await waitFor(() => expect(put()).toBeDefined());
    expect(put()?.path).toBe("/api/v1/objects/c1");
    expect(put()?.body).toEqual({ title: "Q3 orders", expected_version: 4 });
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Chat title" })).not.toBeInTheDocument(),
    );
  });

  it("the rail row follows the rename the header made", async () => {
    script();
    renderChat();

    await openTitle("Yesterday's orders");
    fireEvent.change(field(), { target: { value: "Q3 orders" } });
    fireEvent.keyDown(field(), { key: "Enter" });

    // Both readings of the title move: the header's own, and the rail row that
    // never carried an editor.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Rename chat: Q3 orders" })).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /Q3 orders/, current: "page" })).toBeInTheDocument();
  });

  it("a refused rename puts the chat's name back and says why", async () => {
    script({ putStatus: 409 });
    renderChat();

    await openTitle("Yesterday's orders");
    fireEvent.change(field(), { target: { value: "Q3 orders" } });
    fireEvent.keyDown(field(), { key: "Enter" });

    expect(await screen.findByRole("alert")).toHaveTextContent(/changed underneath you/);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Rename chat: Yesterday's orders" }),
      ).toBeInTheDocument(),
    );
  });

  it("a Can view reader gets a plain title with nothing to press", async () => {
    script({ canWrite: false });
    renderChat();

    await screen.findByRole("heading", { level: 1, name: "Yesterday's orders" });
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Rename chat/ })).not.toBeInTheDocument(),
    );
    expect(put()).toBeUndefined();
  });
});

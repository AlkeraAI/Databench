// "Save as template…" in the chat header's three-dot menu, per shell.
//
// Saving the open chat as a template earns no key of its own on the header bar
// — it is asked for once a chat is worth repeating, not every turn — so the
// three-dot menu is where it lives. The header is the SAME component in both
// shells, and the row must not appear in the editor's webview: a template is a
// copy of the chat's files into a DRIVE, which is the one thing that webview
// has no portal API for. The half a rendering assertion alone would miss is the
// other one: the editor must not ask the portal for the reads that would decide
// it either.
//
// Both halves come off the same fixture, so the divergence is stated once.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const DRIVE = "drv_1";
const NODE = "nd_chat";
const TITLE = "Quarterly plan";
const SAVE = "Save as template…";
const VSCODE = "Open in VS Code";

const { ds, shell } = vi.hoisted(() => {
  /** Which shell is mounted. The surface reads it through the host port,
   *  exactly as the composition does at runtime. */
  const shell = { kind: "browser" as "browser" | "vscode" };
  const ds = {
    listChats: async () => [{ id: "c1", title: TITLE, updatedAt: "2026-09-06T12:00:00Z" }],
    listModels: async () => [],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
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
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
  };
  return { ds, shell };
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const browser = real.createBrowserChatHost({
    account: () => ({ email: "analyst@acme.example", webAppUrl: null }),
  });
  const editor = {
    ...browser,
    kind: "vscode" as const,
    runCommand: async () => undefined,
    openFile: async () => undefined,
  };
  return {
    ...real,
    chatHost: () => (shell.kind === "vscode" ? editor : browser),
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

/** Every request the surface made, so the editor's silence can be asserted. */
let asked: { method: string; path: string; body: unknown }[] = [];
/** Whether the chat has a node in the drive at all. */
let filesNodeId: string | null = NODE;

/** `rename` is the portal's own seam: `ChatPage` hands the surface a rename for
 *  the chat it has read the rung on, and the editor's webview hands it nothing.
 *  Both are passed here exactly as their shells pass them. */
function renderChat(rename = true): void {
  asked = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const asRequest = input instanceof Request ? input : null;
      const raw = asRequest ? asRequest.url : String(input);
      const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
      const { pathname } = new URL(raw, "http://localhost");
      const text = asRequest ? await asRequest.clone().text() : String(init?.body ?? "");
      asked.push({ method, path: pathname, body: text === "" ? null : JSON.parse(text) });
      const json = (payload: unknown, code = 200): Response =>
        new Response(JSON.stringify(payload), {
          status: code,
          headers: { "content-type": "application/json" },
        });
      if (pathname === "/api/v1/chats/c1") {
        return json({ id: "c1", title: TITLE, owner_user_id: "u1", files_node_id: filesNodeId, can_delete: true, can_send: true });
      }
      if (pathname === "/api/v1/files/drives") return json({ id: DRIVE, rootId: "nd_root" });
      if (pathname === "/api/v1/chat-templates") {
        return json({ id: "tpl_1", title: TITLE, version: 1 }, 201);
      }
      if (pathname.includes("/items/")) {
        return json({
          id: NODE,
          driveId: DRIVE,
          kind: "folder",
          name: "c1.alkerachat",
          nameDisplay: "c1.alkerachat",
          parentId: "nd_root",
          etag: "1",
          ctag: "1",
          object: { id: "c1", type: "chat", title: TITLE },
          capabilities: { can_read: true, can_write: true, can_share: true, refusals: {} },
        });
      }
      return json({});
    }),
  );
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route
            path="/chat/:chatId"
            element={
              <ChatSurface chatId="c1" {...(rename ? { onRenameTitle: async () => {} } : {})} />
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const moreKey = (): Promise<HTMLElement> => screen.findByRole("button", { name: "More" });
const rows = (): string[] =>
  Array.from(document.querySelectorAll(".chat-menu__panel [role='menuitem']")).map(
    (row) => row.textContent ?? "",
  );

afterEach(() => {
  cleanup();
  shell.kind = "browser";
  filesNodeId = NODE;
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("the chat header's overflow menu in a browser tab", () => {
  it("offers Save as template… beside the chat's rename", async () => {
    renderChat();
    fireEvent.click(await moreKey());
    await waitFor(() => expect(rows()).toContain(SAVE));
    // The open portal ships no editor hand-off; the product adds it through CHAT_MENU_ACTIONS.
    expect(rows()).toEqual(["Rename", SAVE]);
  });

  it("the row opens the dialog and saves the open chat", async () => {
    renderChat();
    fireEvent.click(await moreKey());
    await waitFor(() => expect(rows()).toContain(SAVE));
    fireEvent.click(screen.getByRole("menuitem", { name: SAVE }));

    const dialog = await screen.findByRole("dialog");
    // Named after the chat, not the `<uuid>.alkerachat` folder it is stored in.
    await waitFor(() =>
      expect((within(dialog).getByLabelText("Name") as HTMLInputElement).value).toBe(TITLE),
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Save template" }));

    await waitFor(() =>
      expect(asked.filter((c) => c.method === "POST" && c.path === "/api/v1/chat-templates")).toHaveLength(1),
    );
    expect(asked.find((c) => c.path === "/api/v1/chat-templates")?.body).toEqual({
      source_chat_id: "c1",
    });
  });

  it("offers no such row for a chat with no node in the drive", async () => {
    filesNodeId = null;
    renderChat();
    // The menu still exists for the rename; the row that needs a node does not.
    fireEvent.click(await moreKey());
    await waitFor(() => expect(rows().length).toBeGreaterThan(0));
    expect(rows()).toEqual(["Rename"]);
  });
});

describe("the same header in the editor's webview", () => {
  it("offers no Save as template…, and asks the portal for nothing", async () => {
    shell.kind = "vscode";
    renderChat(false);
    // The editor's header carries no rename seam either, so there is no menu to
    // hold a row — which is the assertion: nothing was put there.
    await waitFor(() => expect(screen.getByText(TITLE)).toBeInTheDocument());
    expect(screen.queryByRole("menuitem", { name: SAVE })).toBeNull();
    // Nor "Open in VS Code": this header is already in VS Code.
    expect(screen.queryByRole("menuitem", { name: VSCODE })).toBeNull();
    expect(asked.some((c) => c.path.startsWith("/api/v1/files/drives"))).toBe(false);
    expect(asked.some((c) => c.path === "/api/v1/chat-templates")).toBe(false);
  });
});

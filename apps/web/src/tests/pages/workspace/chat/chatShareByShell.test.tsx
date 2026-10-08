// Sharing the chat, per shell.
//
// The header is the SAME component in both shells, so a key added for the
// portal's sake must not appear in the editor's webview — which has no Files
// view to share into and no portal API behind the reads that would decide
// whether it could. The seam is optional: the editor's route table names no
// sharing, so the webview passes nothing, carries no key, and — the half that
// a rendering assertion alone would miss — asks the portal for nothing either.
//
// Both halves are asserted off the same fixture, so the divergence is stated in
// one place rather than inferred from two files.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const { ds, shell } = vi.hoisted(() => {
  /** Which shell is mounted. The surface reads it through the host port,
   *  exactly as the composition does at runtime. */
  const shell = { kind: "browser" as "browser" | "vscode" };
  const ds = {
    listChats: async () => [{ id: "c1", title: "Ops", updatedAt: "2026-09-06T12:00:00Z" }],
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
    account: () => ({ email: "analyst@tideline.example", webAppUrl: null }),
  });
  // The editor's host as the composition sees it: the same port, answering
  // "vscode" and carrying the verbs a webview has.
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

afterEach(() => {
  cleanup();
  shell.kind = "browser";
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

/** Everything the portal would need to answer Yes to "can this be shared?" —
 *  so a missing key is the shell's doing and nothing else. */
function renderChat(): string[] {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const raw = input instanceof Request ? input.url : String(input);
      const { pathname } = new URL(raw, "http://localhost");
      asked.push(pathname);
      const json = (payload: unknown): Response =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (pathname === "/api/v1/chats/c1") {
        return json({ id: "c1", title: "Ops", files_node_id: "nd_chat" });
      }
      if (pathname === "/api/v1/files/drives") return json({ id: "drv_1", rootId: "nd_root" });
      return json({});
    }),
  );
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatSurface chatId="c1" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return asked;
}

const actionNames = (): string[] =>
  Array.from(document.querySelectorAll(".chat-chrome__actions button")).map(
    (key) => key.getAttribute("aria-label") ?? "",
  );

describe("the chat header's Share key, per shell", () => {
  it("is offered in a browser tab, which has a drive to share into", async () => {
    renderChat();
    expect(await screen.findByRole("button", { name: "Share" })).toBeInTheDocument();
  });

  it("is absent in the editor's webview, and asks the portal for nothing", async () => {
    shell.kind = "vscode";
    const asked = renderChat();

    // The header has finished building — the editor's own keys are on it —
    // so Share is missing because no seam was wired, not because nothing has
    // rendered yet.
    await waitFor(() => expect(actionNames()).toContain("New chat"));
    expect(screen.queryByRole("button", { name: "Share" })).toBeNull();
    // The reads behind the key never ran: a webview has no portal API to make
    // them against.
    expect(asked.some((path) => path.startsWith("/api/v1/files"))).toBe(false);
  });
});

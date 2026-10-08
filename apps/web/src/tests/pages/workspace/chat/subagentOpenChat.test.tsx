// The door on a subagent's card, per shell.
//
// The card is shared verbatim by both shells, and so is everything above it —
// only the shell differs. The editor opens a child session as its own tab
// beside the parent, so the door belongs there. A browser tab has no such
// surface: `/chat/<child>` REPLACES the chat the reader is reading, with the
// parent's own transcript nowhere in the crumb trail and no way back to the
// turn that spawned it. So in a tab the card reports the child and offers no
// door — while still reporting it, which is the half that must not be lost.
//
// Both halves are asserted here off the SAME transcript, so the divergence is
// stated in one place rather than inferred from two files.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const { ds, shell } = vi.hoisted(() => {
  // Which shell is mounted. A test sets it before rendering; the surface reads
  // it through the host port, exactly as the composition does at runtime.
  const shell = { kind: "browser" as "browser" | "vscode" };
  const ds = {
    listChats: async () => [
      { id: "c1", title: "Ops", updatedAt: "2026-09-06T12:00:00Z", permissionMode: "read_only" },
    ],
    listModels: async () => [],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: async () => [],
    runCommand: async () => ({ kind: "cli_only", command: null, payload: {}, message: "" }),
    listContext: async () => ({ total: 0 }),
    lineageRoots: async () => ({}),
    // One settled delegation, with the child session the door would lead to.
    getChatTurns: async () => [
      {
        id: "a1",
        author: "assistant",
        status: "completed",
        parts: [
          {
            kind: "subagent",
            id: "s1",
            name: "analyst",
            agent: "analyst",
            status: "completed",
            instructions: "Check the churn numbers",
            summary: "Churn is 3.2% and falling",
            childSessionId: "child-1",
          },
        ],
      },
    ],
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

function renderChat(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
    ),
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
}

/** The delegation's card, once the transcript has landed. */
const card = (): Promise<HTMLElement> =>
  waitFor(() => {
    const found = document.querySelector<HTMLElement>("[data-subagent-block]");
    expect(found).not.toBeNull();
    return found as HTMLElement;
  });

describe("a subagent's card in a browser tab", () => {
  it("offers no door into the child's chat", async () => {
    renderChat();

    await card();
    expect(screen.queryByRole("button", { name: /open the analyst chat/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^open chat$/i })).toBeNull();
  });

  it("still reports the child and what it came back with", async () => {
    // The status half of the card is untouched — hiding the door must not cost
    // the reader the delegation itself.
    renderChat();

    const block = await card();
    expect(block.getAttribute("aria-label")).toBe("Report from analyst");
    expect(block.getAttribute("data-status")).toBe("done");
  });
});

describe("the same card in the editor", () => {
  it("keeps the door, because a tab opens beside the chat rather than over it", async () => {
    shell.kind = "vscode";
    renderChat();

    await card();
    expect(screen.getByRole("button", { name: /open the analyst chat/i })).toBeTruthy();
  });
});

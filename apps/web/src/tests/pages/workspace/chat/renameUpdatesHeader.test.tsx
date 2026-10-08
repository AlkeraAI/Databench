// Renaming the open chat renames its header, with nothing reloaded.
//
// The rename mutation is one write with four readers, and they do not share a
// cache entry. The browser tab and the portal's rail read the chat row under
// `keys.chats.*`; the chat's own header and its crumb trail read the list the
// chat composition keeps for itself under `chatKeys.chats()` — a second entry,
// because that composition also runs in the editor's webview and cannot reach
// the portal's cache. `useRenameChat` patched (and invalidated) only the
// portal's entries, so the tab and the rail took the new name while the header
// above the transcript kept the old one until the page was reloaded.
//
// Both halves are pinned here, through the REAL surface: the optimistic patch,
// which puts the new name in the header before any list is re-read, and the
// invalidation, which is what lands the server's own word on that entry.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRenameChat } from "@/api/objects";
import { createQueryClient } from "@/api/queryClient";
import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";
import { useChatStore } from "@/pages/workspace/chat/chatStore";
import type { ChatDataSource } from "@/pages/workspace/chat/data/ChatDataSource";
import {
  createBrowserChatHost,
  installChatRuntime,
  resetChatRuntime,
} from "@/pages/workspace/chat/data";

const CHAT = "c1";
const ASKED = "Hi what model are you?";
const RENAMED = "TEST";

/** The fake server's copy of the title: the PUT moves it, so a re-read answers
 *  the new name the way the real one does. */
let stored: string;
/** Held while a test wants the list read to be IN FLIGHT, so an assertion can
 *  tell the optimistic patch from the answer to a refetch. */
let listGate: Promise<void> | null;
let listChats: ReturnType<typeof vi.fn>;

function sourceOver(): ChatDataSource {
  listChats = vi.fn(async () => {
    if (listGate) await listGate;
    return [{ id: CHAT, title: stored, updatedAt: "2026-09-17T12:00:00Z" }];
  });
  const source = {
    caps: { opencodeActive: false, modelCatalog: false },
    listChats,
    getChatTurns: async () => [],
    subscribeChat: () => () => undefined,
    subscribePermissionMode: () => () => undefined,
    getPermissionMode: async () => "default",
    setPermissionMode: async () => undefined,
    setEffort: async () => undefined,
    listModels: async () => [],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: async () => [],
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    searchFiles: async () => [],
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
    createChat: async () => ({ id: CHAT, title: stored }),
    sendUserMessage: async () => ({}),
    deleteChat: async () => true,
    reopenChat: async () => undefined,
  };
  return source as unknown as ChatDataSource;
}

/** The rename, taken exactly where the page takes it: the one mutation hook the
 *  rail row, the Files row and the header's own editor all call. */
function RenameButton() {
  const rename = useRenameChat();
  return (
    <button type="button" onClick={() => rename.mutate({ chatId: CHAT, title: RENAMED })}>
      rename
    </button>
  );
}

/** The object route, as far as the rename needs it: a GET for the version it
 *  writes against, and a PUT that moves the stored title. */
function stubObjectRoute(): { puts: () => number } {
  let puts = 0;
  const json = (payload: unknown): Response =>
    new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const at = new URL(
        input instanceof Request ? input.url : String(input),
        "http://x",
      ).pathname;
      const method = (input instanceof Request ? input.method : init?.method) ?? "GET";
      if (at === `/api/v1/objects/${CHAT}`) {
        if (method === "PUT") {
          puts += 1;
          stored = String(
            (init?.body ? (JSON.parse(String(init.body)) as { title?: string }) : {}).title ??
              stored,
          );
        }
        return json({ id: CHAT, type: "chat", title: stored, version: 4, spec: {} });
      }
      throw new Error(`unexpected request: ${method} ${at}`);
    }),
  );
  return { puts: () => puts };
}

function mount() {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/chat/${CHAT}`]}>
        <Routes>
          <Route
            path="/chat/:chatId"
            element={
              <>
                <RenameButton />
                <ChatSurface chatId={CHAT} />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** What the chrome is calling this chat, wherever it draws the name — the
 *  heading itself, or the last crumb of the trail when the chat carries one. */
const headerName = (): string[] =>
  Array.from(document.querySelectorAll(".chat-chrome__id")).map((el) => el.textContent ?? "");

beforeEach(() => {
  stored = ASKED;
  listGate = null;
  installChatRuntime({
    source: sourceOver(),
    host: createBrowserChatHost({ account: () => ({ email: "dana@example.com", webAppUrl: null }) }),
  });
});

afterEach(() => {
  cleanup();
  resetChatRuntime();
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("renaming the open chat", () => {
  it("puts the new name in the header before any list is re-read", async () => {
    const route = stubObjectRoute();
    mount();
    await waitFor(() => expect(headerName().join("|")).toContain(ASKED));

    // Every later read of the list is held open, so nothing but the mutation's
    // own patch can move what the header is showing.
    let release!: () => void;
    listGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const reads = listChats.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "rename" }));

    await waitFor(() => expect(headerName().join("|")).toContain(RENAMED));
    expect(headerName().join("|")).not.toContain(ASKED);
    // The write landed, and the header did not wait for a re-read to say so.
    expect(route.puts()).toBe(1);
    expect(listChats.mock.calls.length).toBeLessThanOrEqual(reads + 1);

    release();
  });

  it("re-reads the surface's own list so the server's word lands there too", async () => {
    stubObjectRoute();
    mount();
    await waitFor(() => expect(headerName().join("|")).toContain(ASKED));
    const reads = listChats.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "rename" }));

    // The entry the header reads is invalidated, not only the portal's, so the
    // name on screen is the one the row actually holds.
    await waitFor(() => expect(listChats.mock.calls.length).toBeGreaterThan(reads));
    await waitFor(() => expect(headerName().join("|")).toContain(RENAMED));
    // And it stays: the re-read answers the new title rather than putting the
    // old one back over the patch.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(headerName().join("|")).toContain(RENAMED);
    expect(headerName().join("|")).not.toContain(ASKED);
  });

  it("puts the old name back when the write is refused", async () => {
    // The refusal is held until the optimistic name is on screen, so the
    // rollback is a state the test actually observes rather than one it assumes
    // never happened.
    let refuse!: () => void;
    const refused = new Promise<void>((resolve) => {
      refuse = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const method = (input instanceof Request ? input.method : init?.method) ?? "GET";
        if (method !== "PUT") {
          return new Response(
            JSON.stringify({ id: CHAT, type: "chat", title: stored, version: 4, spec: {} }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        await refused;
        return new Response(
          JSON.stringify({ detail: { code: "forbidden", message: "You cannot rename this chat." } }),
          { status: 403, headers: { "content-type": "application/json" } },
        );
      }),
    );
    mount();
    await waitFor(() => expect(headerName().join("|")).toContain(ASKED));

    fireEvent.click(screen.getByRole("button", { name: "rename" }));
    await waitFor(() => expect(headerName().join("|")).toContain(RENAMED));

    refuse();

    // The patch is rolled back on the surface's entry as well, so a refused
    // rename never leaves the header claiming a name the row refused.
    await waitFor(() => expect(headerName().join("|")).toContain(ASKED));
    expect(headerName().join("|")).not.toContain(RENAMED);
  });
});

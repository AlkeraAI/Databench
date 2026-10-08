// Only a press of Stop stops a turn.
//
// An owner answered a permission ask and read, moments later, that the turn had
// been stopped by them. Nothing they had pressed was Stop. So every way an ask
// is answered — the Allow key, the Deny key, Enter, Escape — and every way a
// tab goes away are run here through the REAL source over a fake wire, and
// what is counted is the requests that actually left the browser: the answer
// goes, and no stop goes with it.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConversationTurn } from "@alkera/chat-model";
import { createQueryClient } from "@/api/queryClient";

const CHAT = "c1";
const REQUEST = "per_0c3e3bc7d001YdngCqsLUGX2Mw";

function askingTurn(): ConversationTurn {
  return {
    id: "a1",
    author: "assistant",
    parts: [
      {
        kind: "permission",
        id: REQUEST,
        requestId: REQUEST,
        permissionKind: "edit",
        canonicalKind: "edit",
        patterns: ["scratch/summary-dashboard.sql"],
        status: "pending",
        prompting: true,
        options: [
          { optionId: "allow_once", name: "Allow once" },
          { optionId: "allow_always", name: "Always allow" },
          { optionId: "reject_once", name: "Reject once" },
          { optionId: "reject_always", name: "Always reject" },
        ],
      },
    ],
  } as unknown as ConversationTurn;
}

/** The wire: the source's own transport under it, the answer and the stop both
 *  leaving the browser as real posts. */
const { ds } = vi.hoisted(() => ({
  ds: {
    listChats: async () => [{ id: "c1", title: "Summary dashboard", updatedAt: "2026-09-21T12:00:00Z" }],
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: async () => [] as unknown[],
    searchFiles: async () => [] as unknown[],
    sendUserMessage: async (_chatId: string, content: string) => ({ id: "m1", role: "user", content }),
    resolvePermission: (chatId: string, requestId: string, optionId: string) =>
      new CloudDataSource().resolvePermission(chatId, requestId, optionId),
    cancelTurn: (chatId: string) => new CloudDataSource().cancelTurn(chatId),
    mayAllow: () => ({ allowed: true }),
    turnState: () => "working" as const,
    getPermissionMode: async () => "default",
    setPermissionMode: async () => {},
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
  },
}));

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const host = real.createBrowserChatHost({
    account: () => ({ email: "owner@tideline.example", webAppUrl: null }),
  });
  return {
    ...real,
    chatHost: () => host,
    chatData: () => ds,
    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
    chatCaps: () => ({ opencodeActive: false, modelCatalog: true }),
  };
});

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";
import { useChatStore } from "@/pages/workspace/chat/chatStore";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

/** Every request the browser posted: its path and, for an answer, the body. */
const posted: { path: string; body: Record<string, unknown> | null }[] = [];
const beacons: string[] = [];

function stubWire(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input instanceof Request ? input.url : input);
      const path = url.replace(/^https?:\/\/[^/]+/, "");
      const body = typeof init?.body === "string" ? (JSON.parse(init.body) as Record<string, unknown>) : null;
      if (path.endsWith("/answer") || path.endsWith("/stop")) {
        posted.push({ path, body });
        return new Response(null, { status: 202 });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  Object.defineProperty(navigator, "sendBeacon", {
    configurable: true,
    value: vi.fn((url: string | URL) => {
      beacons.push(String(url));
      return true;
    }),
  });
}

function renderChat(): void {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:id" element={<ChatSurface chatId={CHAT} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const stops = () => posted.filter((request) => request.path.endsWith("/stop"));
const answers = () => posted.filter((request) => request.path.endsWith("/answer"));

async function card(): Promise<HTMLElement> {
  const allow = await screen.findByRole("button", { name: /Allow once/ });
  const section = allow.closest("section");
  if (!section) throw new Error("the permission card has no section");
  return section;
}

beforeEach(() => {
  posted.length = 0;
  beacons.length = 0;
  ds.getChatTurns = async () => [askingTurn()];
  useChatStore.setState({ byId: {}, composerPrefs: {} });
  stubWire();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("answering an ask", () => {
  it("posts the answer and nothing else when Allow is pressed", async () => {
    renderChat();
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: /Allow once/ }));
    });
    await waitFor(() => expect(answers()).toHaveLength(1));
    expect(answers()[0].body).toMatchObject({ interrupt_id: REQUEST, option_id: "allow_once" });
    // The card settles and the composer takes the slot while the turn still
    // runs — and still nothing but the answer has gone.
    await waitFor(() => expect(screen.queryByRole("button", { name: /Allow once/ })).toBeNull());
    expect(stops()).toEqual([]);
  });

  it("Enter on the card allows, and only allows", async () => {
    renderChat();
    const allow = await screen.findByRole("button", { name: /Allow once/ });
    await act(async () => {
      fireEvent.keyDown(allow, { key: "Enter" });
    });
    await waitFor(() => expect(answers()).toHaveLength(1));
    expect(answers()[0].body).toMatchObject({ option_id: "allow_once" });
    expect(stops()).toEqual([]);
  });

  it("Escape on the card denies, and only denies", async () => {
    renderChat();
    const section = await card();
    await act(async () => {
      fireEvent.keyDown(section, { key: "Escape" });
    });
    await waitFor(() => expect(answers()).toHaveLength(1));
    expect(answers()[0].body).toMatchObject({ option_id: "reject_once", reject: false });
    expect(stops()).toEqual([]);
  });

  it("the Deny key denies, and only denies", async () => {
    renderChat();
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: /Reject once/ }));
    });
    await waitFor(() => expect(answers()).toHaveLength(1));
    expect(answers()[0].body).toMatchObject({ option_id: "reject_once" });
    expect(stops()).toEqual([]);
  });
});

describe("the tab going away", () => {
  it("sends no stop on pagehide, unload or the tab hiding", async () => {
    renderChat();
    await card();
    await act(async () => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("pagehide"));
      window.dispatchEvent(new Event("beforeunload"));
      window.dispatchEvent(new Event("unload"));
    });
    expect(stops()).toEqual([]);
    expect(beacons).toEqual([]);
    expect(answers()).toEqual([]);
  });
});

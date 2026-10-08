// The rail's mark on a chat is the server's status for it, and nothing else.
//
// The page knows a turn has started or ended (a send pressed, the open chat's
// publisher) before the server's chat list does. It uses that only to read the
// list again, so the mark follows the server's new word at once; it never
// draws a light of its own. A chat the server has no status for, or a resting
// one, draws nothing.

import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { StatusFact } from "@/api/status";

// The transcript is proven by its own suite; this file is about the rail beside
// it, so the surface is a marker that mounts nothing.
vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: () => <div data-testid="chat-surface" />,
}));

// Nor does the rail need a live roster: the faces are their own suite, and
// mounting them here would open a socket for every case.
vi.mock("@/pages/workspace/chat/ChatPresence", () => ({
  ChatPresence: () => null,
}));

/** What the page's data source says about each chat's turn, and who it tells. */
const turns = new Map<string, "working" | "idle">();
const listeners = new Map<string, Set<() => void>>();

vi.mock("@/pages/workspace/chat/data/CloudDataSource", () => ({
  CloudDataSource: class {
    caps = {};
    turnState(chatId: string): "working" | "idle" | null {
      return turns.get(chatId) ?? null;
    }
    subscribeChat(chatId: string, onEvent: () => void): () => void {
      const set = listeners.get(chatId) ?? new Set<() => void>();
      set.add(onEvent);
      listeners.set(chatId, set);
      return () => set.delete(onEvent);
    }
  },
}));

import { ChatPage } from "@/pages/workspace/chat/ChatPage";

const WORKING: StatusFact = {
  subject: "chat",
  state: "working",
  label: "Working",
  tone: "info",
  reason_code: "",
  sentence: "The agent is working.",
};

const AWAKE: StatusFact = {
  subject: "chat",
  state: "awake",
  label: "Awake",
  tone: "success",
  reason_code: "",
  sentence: "Ready for a message.",
};

const CHAT = {
  id: "c1",
  title: "Yesterday's orders",
  machine_id: "m1",
  machine_status: "ready",
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  last_seq: 3,
};

/** The status the server writes for the chat right now. */
let serverStatus: StatusFact | null = null;

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function scriptFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = { ...CHAT, status: serverStatus };
      if (/\/api\/v1\/chats\/[^/?]+$/.test(new URL(url, "http://x").pathname)) return json(body);
      if (url.includes("/api/v1/chats")) return json({ items: [body], next_cursor: null });
      if (url.includes("/api/v1/machines/current")) {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      if (url.includes("/api/v1/auth/me")) return json({ id: "u1", email: "dana@example.com" });
      return json({});
    }),
  );
}

function renderChat(): void {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function announceTurn(state: "working" | "idle"): void {
  turns.set("c1", state);
  act(() => {
    for (const listener of listeners.get("c1") ?? []) listener();
  });
}

const rail = (): HTMLElement => screen.getByRole("navigation", { name: /chats/i });

beforeEach(() => {
  turns.clear();
  listeners.clear();
  serverStatus = null;
  scriptFetch();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("the status mark on a rail row", () => {
  it("draws nothing for a chat with no server status, even mid-turn", async () => {
    turns.set("c1", "working");
    renderChat();

    const row = await within(rail()).findByRole("button", { name: /^Yesterday's orders/ });
    await waitFor(() => expect(within(row).queryAllByRole("img")).toHaveLength(0));
    expect(row.textContent).toBe("Yesterday's orders");
  });

  it("draws the server's working status once the open chat's turn starts", async () => {
    serverStatus = AWAKE;
    renderChat();
    const row = await within(rail()).findByRole("button", { name: /^Yesterday's orders/ });
    expect(within(row).queryByRole("img")).toBeNull();

    // The server now says working; the publisher's word is what prompts the
    // rail to read it, with no poll in between.
    serverStatus = WORKING;
    announceTurn("working");

    const dot = await within(row).findByRole("img", { name: "Working" });
    expect(dot).toHaveAttribute("data-tone", "info");
    expect(dot).toHaveAttribute("title", "The agent is working.");
  });

  it("clears the mark once the turn ends and the server says the chat rests", async () => {
    serverStatus = WORKING;
    turns.set("c1", "working");
    renderChat();
    const row = await within(rail()).findByRole("button", { name: /^Yesterday's orders/ });
    await within(row).findByRole("img", { name: "Working" });

    serverStatus = AWAKE;
    announceTurn("idle");

    await waitFor(() => expect(within(row).queryAllByRole("img")).toHaveLength(0));
  });
});

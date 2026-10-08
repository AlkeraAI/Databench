// A chat reopened while its turn is still running reads as running.
//
// The working line and Stop came only from what THIS tab had done: the tab that
// sent the message showed "Working…" and a Stop; a reopen of the same chat — a
// reload, a second tab, a colleague — showed the prompt, no working line, and a
// Send, while the server said the turn was running and the box kept writing
// `running` into the transcript. The reader could not stop the turn from the
// reopened page at all. The machine's own word now opens the turn as well as
// holding it.
//
// Two halves: the surface, which must show the working line and a live Stop
// from the machine's word alone; and the cloud source, which must give that
// word from the durable record before the document has spoken.

import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { DocHandle, DocMessage } from "@/api/realtime/docSync";

const { ds, state } = vi.hoisted(() => {
  const state = {
    turnState: null as "working" | "idle" | null,
    turns: [] as unknown[],
  };
  const ds = {
    turnState: () => state.turnState,
    listChats: async () => [{ id: "c1", title: "Chat", updatedAt: "2026-09-28T03:54:40Z" }],
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: async () => state.turns,
    searchFiles: async () => [] as unknown[],
    sendUserMessage: vi.fn(async () => ({ id: "m1", role: "user", content: "x" })),
    cancelTurn: vi.fn(async () => ({ stopped: true })),
    getPermissionMode: async () => "default",
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
  };
  return { ds, state };
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const host = real.createBrowserChatHost({
    account: () => ({ email: "dana@tideline.example", webAppUrl: null }),
  });
  return {
    ...real,
    chatHost: () => host,
    chatData: () => ds,
    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
    chatCaps: () => ({ opencodeActive: false }),
  };
});

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";
import { useChatStore } from "@/pages/workspace/chat/chatStore";

/** What the durable record folds to mid-turn: the prompt, and the answer the
 *  box opened and has not written a word of yet. */
const MID_TURN = [
  { id: "u1", author: "user", status: "done", parts: [{ id: "u1-t", kind: "text", text: "Create hello.txt" }] },
  { id: "a1", author: "assistant", status: "running", parts: [] },
];

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  state.turnState = null;
  state.turns = [];
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
});

function reopen() {
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

describe("a chat reopened while the machine is working its turn", () => {
  it("shows the working line and a Stop the reader can press, not a Send", async () => {
    state.turns = MID_TURN;
    state.turnState = "working";
    reopen();

    const stop = await screen.findByRole("button", { name: "Stop" });
    expect(stop).toBeEnabled();
    expect(screen.queryByRole("button", { name: /^Send/ })).toBeNull();
    expect(screen.getByText(/^working/i)).toBeInTheDocument();

    await act(async () => {
      stop.click();
    });
    expect(ds.cancelTurn).toHaveBeenCalledWith("c1");
  });

  it("reads as settled once the machine says the turn is over", async () => {
    state.turns = [
      MID_TURN[0],
      { id: "a1", author: "assistant", status: "done", completedAt: "2026-09-28T04:00:00Z", parts: [{ id: "a1-t", kind: "text", text: "Done." }] },
    ];
    state.turnState = "idle";
    reopen();

    expect(await screen.findByRole("button", { name: /^Send/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  });
});

// ── The cloud source: the machine's word before the document has spoken ──────

function silentDoc(): DocHandle<unknown> {
  return {
    onMessage: (_listener: (m: DocMessage<unknown>) => void) => () => undefined,
    onPhase: () => () => undefined,
    getPhase: () => ({ phase: "connecting", epoch: 0, seq: 0, peerId: "p:1", canWrite: false, pending: 0, error: null }),
    sendOp: () => Promise.reject(new Error("a reader never writes to a chat document")),
    dispose: () => undefined,
  } as unknown as DocHandle<unknown>;
}

/** A durable row as the server serves it: the harness event one level in. */
function row(seq: number, event: Record<string, unknown>) {
  const eventId = `e${seq}`;
  return {
    id: `m-${seq}`,
    chat_id: "c1",
    seq,
    role: "system" as const,
    kind: String(event.event_type),
    event_id: eventId,
    payload: { event_id: eventId, role: "system", kind: String(event.event_type), payload: { event_id: eventId, ...event } },
    created_at: "2026-09-28T03:54:42Z",
  };
}

const running = (seq: number) =>
  row(seq, { event_type: "session.status_changed", status: "running", phase: "awaiting_llm", turn_id: "t1" });
const idle = (seq: number) => row(seq, { event_type: "session.status_changed", status: "idle", turn_id: "t1" });

async function sourceOver(rows: ReturnType<typeof row>[]) {
  const { CloudDataSource } = await import("@/pages/workspace/chat/data/CloudDataSource");
  const source = new CloudDataSource({
    rest: {
      listMessages: async (_chatId: string, opts?: { afterSeq?: number }) => {
        const after = opts?.afterSeq ?? 0;
        const items = rows.filter((r) => r.seq > after);
        return { items, next_after_seq: items.at(-1)?.seq ?? after, resync_from: null };
      },
    } as never,
    openDoc: () => silentDoc() as DocHandle<never>,
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
  source.subscribeChat("c1", () => undefined);
  await source.getChatTurns("c1");
  return source;
}

describe("the cloud source on a cold open, before the document has spoken", () => {
  it("reads the turn as working when the record's last session status is running", async () => {
    const source = await sourceOver([running(8), running(13), running(16)]);
    expect(source.turnState("c1")).toBe("working");
  });

  it("says nothing of a turn the record shows ended", async () => {
    const source = await sourceOver([running(8), idle(9)]);
    expect(source.turnState("c1")).not.toBe("working");
  });
});

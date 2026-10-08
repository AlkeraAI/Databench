// The SECOND message of a chat this tab started.
//
// A chat started from the composer is created by a SEND: the source posts the
// first message before it has ever read the chat's durable record. The page
// that hosts the chat builds that source once. Rebuilding it when the signed-in
// account resolves (without remounting the surface) would leave the store
// streaming off the first source while reads and sends go to a second one that
// never read this chat. Both halves are pinned here:
//
//  1. a source whose first act on a chat is a SEND still answers with the whole
//     transcript, so a reader never loses the conversation they are in; and
//  2. the chat page keeps ONE source for its life, so the subscription and the
//     reads can never be on different ones.

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { DocHandle } from "@/api/realtime/docSync";
import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";
import { useChatStore } from "@/pages/workspace/chat/chatStore";
import { createBrowserChatHost, installChatRuntime, resetChatRuntime } from "@/pages/workspace/chat/data";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

const CHAT = "c1";
const AT = "2026-09-17T12:00:00Z";
const ASKED = "Hi what model are you?";
const ANSWERED = "I run on Claude.";
const THEN = "oh";

type DocMessage = { kind: string; payload?: unknown; ephemeral?: boolean; state?: unknown };

/** A chat document the test drives: every frame it pushes reaches the source. */
function scriptedDoc(): { handle: DocHandle<never>; push: (message: DocMessage) => void } {
  const listeners = new Set<(message: DocMessage) => void>();
  const handle = {
    onMessage: (listener: (message: DocMessage) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    onPhase: () => () => undefined,
    getPhase: () => ({
      phase: "live",
      epoch: 1,
      seq: 0,
      peerId: "p:1",
      canWrite: false,
      pending: 0,
      error: null,
    }),
    sendOp: () => Promise.reject(new Error("a reader never writes to a chat document")),
    dispose: () => undefined,
  } as unknown as DocHandle<never>;
  return { handle, push: (message) => listeners.forEach((listener) => listener(message)) };
}

/** The row the server writes for a person's message — flat, no `event_type`. */
function promptRow(seq: number, clientId: string, text: string) {
  return {
    id: `row-${seq}`,
    chat_id: CHAT,
    seq,
    role: "user" as const,
    kind: "prompt",
    event_id: `usr:${clientId}`,
    payload: {
      schema_version: "1.2.0",
      kind: "prompt",
      text,
      client_id: clientId,
      user_id: "u1",
      attachments: [],
      context: "",
    },
    created_at: AT,
  };
}

/** A machine's transcript entry: the envelope, with the event one level in. */
function entryOf(role: "user" | "assistant", event: Record<string, unknown>) {
  return { event_id: String(event.event_id), role, kind: String(event.event_type), payload: event };
}

/** What the harness publishes when it takes a person's message: it says the
 *  words back on a `user` message of its own. */
function echoEvents(text: string, messageId: string): Record<string, unknown>[] {
  return [
    { event_id: `${messageId}-created`, event_type: "message.created", message_id: messageId, role: "user" },
    {
      event_id: `${messageId}-part`,
      event_type: "part.created",
      message_id: messageId,
      part: { part_id: `${messageId}-t`, message_id: messageId, type: "text", text },
    },
  ];
}

function answerEvents(text: string, messageId: string): Record<string, unknown>[] {
  return [
    { event_id: `${messageId}-created`, event_type: "message.created", message_id: messageId, role: "assistant" },
    {
      event_id: `${messageId}-part`,
      event_type: "part.created",
      message_id: messageId,
      part: { part_id: `${messageId}-t`, message_id: messageId, type: "text", text },
    },
    { event_id: `${messageId}-done`, event_type: "message.completed", message_id: messageId },
  ];
}

function appendOp(events: Record<string, unknown>[], role: "user" | "assistant") {
  return {
    kind: "op",
    ephemeral: false,
    payload: {
      op_id: `op-${String(events[0]?.event_id)}`,
      intent: "append",
      events: events.map((event) => entryOf(role, event)),
    },
  };
}

/** One machine event as the durable record stores it. */
function machineRow(seq: number, role: "user" | "assistant", event: Record<string, unknown>) {
  return {
    id: `row-${seq}`,
    chat_id: CHAT,
    seq,
    role,
    kind: String(event.event_type),
    event_id: String(event.event_id),
    payload: entryOf(role, event),
    created_at: AT,
  };
}

/** Everything the server holds for this chat once the first turn has finished. */
function durableAfterTheFirstTurn(): Record<string, unknown>[] {
  const rows: Record<string, unknown>[] = [promptRow(1, "web-1", ASKED)];
  let seq = 2;
  for (const event of echoEvents(ASKED, "hm1")) rows.push(machineRow(seq++, "user", event));
  for (const event of answerEvents(ANSWERED, "a1")) rows.push(machineRow(seq++, "assistant", event));
  return rows;
}

/** A source over one scripted document and one durable record. */
function sourceOver(doc: ReturnType<typeof scriptedDoc>, rows: Record<string, unknown>[]) {
  let client = 0;
  // A durable record short enough that its newest page IS the whole record:
  // the tail a cold open reads and the forward read after it both come from
  // here, keyed the way the server keys them.
  const listMessages = vi.fn(async (_chatId: string, opts: { afterSeq?: number; tail?: boolean }) => {
    const after = opts.tail ? 0 : (opts.afterSeq ?? 0);
    const items = rows.filter((row) => (row.seq as number) > after);
    return {
      items,
      next_after_seq: items.length ? (items[items.length - 1].seq as number) : after,
      resync_from: null,
      prev_before: items.length ? (items[0].seq as number) : null,
      has_older: false,
    };
  });
  const postMessage = vi.fn();
  const rest = {
    listMessages,
    postMessage,
    createChat: vi.fn(async () => ({ id: CHAT, title: "Hi", updated_at: AT, permission_mode: "read_only" })),
    listChats: vi.fn(async () => ({ items: [], next_cursor: null })),
    chatModels: vi.fn(async () => ({ items: [] })),
    chatDefaults: vi.fn(async () => ({ model: null, effort: null, permission_mode: "read_only" })),
    getChat: vi.fn(async () => ({ id: CHAT, title: "Hi", updated_at: AT, permission_mode: "read_only" })),
  };
  const source = new CloudDataSource({
    rest: rest as never,
    openDoc: () => doc.handle,
    acquire: () => () => undefined,
    clientId: () => `web-${++client}`,
  });
  return { source, rest, listMessages, postMessage };
}

function install(source: CloudDataSource): void {
  installChatRuntime({
    source,
    host: createBrowserChatHost({ account: () => ({ email: "dana@example.com", webAppUrl: null }) }),
  });
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function mountChat() {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/chat/${CHAT}`]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatSurface chatId={CHAT} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  resetChatRuntime();
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
  vi.clearAllMocks();
});

describe("the second message of a chat this tab started", () => {
  it("keeps the first exchange and says the new message once", async () => {
    const doc = scriptedDoc();
    // The source the chat was STARTED on: its very first act was the send that
    // created the chat, so it has never read this chat's durable record.
    const first = sourceOver(doc, durableAfterTheFirstTurn());
    install(first.source);
    first.postMessage.mockResolvedValueOnce(promptRow(1, "web-1", ASKED));
    await first.source.createChat(ASKED);

    mountChat();
    await waitFor(() => expect(screen.getAllByText(ASKED).length).toBeGreaterThan(0));
    doc.push({ kind: "snapshot", state: { events: [], meta: {} } });
    doc.push(appendOp(echoEvents(ASKED, "hm1"), "user"));
    doc.push(appendOp(answerEvents(ANSWERED, "a1"), "assistant"));
    await waitFor(() => expect(screen.getByText(ANSWERED)).toBeInTheDocument());

    // The page's runtime is rebuilt — the signed-in account resolved — while
    // the surface stays mounted, so nothing re-opens the chat: the store's
    // subscription is still on the first source.
    const second = sourceOver(doc, durableAfterTheFirstTurn());
    install(second.source);
    second.postMessage.mockResolvedValueOnce(promptRow(6, "web-1", THEN));

    await act(async () => {
      useChatStore.getState().send(CHAT, THEN);
      await tick();
      // The box takes the turn and says the words back on the live document,
      // which the FIRST source still holds — so the transcript is re-read.
      doc.push(appendOp(echoEvents(THEN, "hm2"), "user"));
      await tick();
      await tick();
    });

    expect(screen.getAllByText(ASKED).length, "the question that opened the chat is still there").toBeGreaterThan(0);
    expect(screen.getByText(ANSWERED), "and so is the answer to it").toBeInTheDocument();
    expect(screen.getAllByText(THEN), "the new message is said once, not twice").toHaveLength(1);
    expect(screen.getByText(/^working/i), "the new turn is still going").toBeInTheDocument();
  });

  it("reads the whole record on a source whose only act on the chat was a send", async () => {
    // The unit underneath the case above: the row the server writes for OUR
    // send names a sequence far ahead of anything this source has read, and
    // taking it for the read cursor declared the history below it read when it
    // never was — so the transcript came back holding that one message.
    const doc = scriptedDoc();
    const { source, postMessage } = sourceOver(doc, durableAfterTheFirstTurn());
    postMessage.mockResolvedValueOnce(promptRow(6, "web-1", THEN));

    await source.sendUserMessage(CHAT, THEN);
    const turns = await source.getChatTurns(CHAT);

    const said = turns
      .filter((turn) => turn.author === "user")
      .flatMap((turn) => turn.parts.filter((part) => part.kind === "text").map((part) => part.text));
    expect(said, "the question that opened the chat came back with it").toContain(ASKED);
    expect(said.filter((text) => text === THEN), "and the sent message once").toHaveLength(1);
    expect(JSON.stringify(turns), "with the answer it was given").toContain(ANSWERED);
  });
});

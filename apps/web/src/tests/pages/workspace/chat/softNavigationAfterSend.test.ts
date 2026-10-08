// Clicking back into a chat you just sent in shows the message once.
//
// A hard reload rebuilds the transcript from the server's page, where the
// person's message is the row before the box's echo of it. In-app navigation
// does not: the source and its fold are a singleton that outlive the route, so
// what the rail's click re-reads is whatever the LIVE stream built — and live,
// the echo and the first answer can reach the browser before the send's own
// response has put the message on screen. The message was then in the
// transcript twice: once as the box heard it, once more under the answer,
// still reading as going out.

import { beforeEach, describe, expect, it, vi } from "vitest";

import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import { installChatRuntime, resetChatRuntime, type ChatHost } from "@/pages/workspace/chat/data";
import { useChatStore } from "@/pages/workspace/chat/chatStore";
import type { DocHandle } from "@/api/realtime/docSync";

const NOW = "2026-09-16T12:00:00Z";
const CHAT = "c1";
const SAID = "who are you?";

type DocMessage = { kind: string; payload?: unknown; ephemeral?: boolean; state?: unknown };

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

function promptRow(text: string) {
  return {
    id: "row-1",
    chat_id: CHAT,
    seq: 1,
    role: "user" as const,
    kind: "prompt",
    event_id: "usr:web-1",
    payload: {
      schema_version: "1.2.0",
      kind: "prompt",
      text,
      client_id: "web-1",
      user_id: "u1",
      attachments: [],
      context: "",
    },
    created_at: NOW,
  };
}

function entryOf(role: "user" | "assistant", event: Record<string, unknown>) {
  return { event_id: String(event.event_id), role, kind: String(event.event_type), payload: event };
}

function appendOp(role: "user" | "assistant", events: Record<string, unknown>[]) {
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

const ECHO = [
  { event_id: "hm1-created", event_type: "message.created", message_id: "hm1", role: "user" },
  {
    event_id: "hm1-part",
    event_type: "part.created",
    message_id: "hm1",
    part: { part_id: "hm1-t", message_id: "hm1", type: "text", text: SAID },
  },
];

const ANSWER = [
  { event_id: "a1-created", event_type: "message.created", message_id: "a1", role: "assistant" },
  {
    event_id: "a1-part",
    event_type: "part.created",
    message_id: "a1",
    part: { part_id: "a1-t", message_id: "a1", type: "text", text: "I'm Claude Opus 5." },
  },
];

function turnsNow(): { author: string; status?: string; parts: { kind: string; text?: string }[] }[] {
  const entry = useChatStore.getState().byId[CHAT];
  return [...(entry?.base ?? []), ...(entry?.optimistic ?? [])];
}

function userSaid(): string[] {
  return turnsNow()
    .filter((turn) => turn.author === "user")
    .flatMap((turn) => turn.parts.filter((p) => p.kind === "text").map((p) => p.text ?? ""));
}

describe("clicking back into a chat after sending in it", () => {
  beforeEach(() => {
    useChatStore.setState({ byId: {}, pending: {} });
    return () => resetChatRuntime();
  });

  it("shows the message once when the box answered before the send returned", async () => {
    const listMessages = vi
      .fn()
      .mockResolvedValue({ items: [], next_after_seq: 0, resync_from: null });
    let answerSend = (_: unknown) => undefined as void;
    const postMessage = vi.fn().mockReturnValue(new Promise((resolve) => (answerSend = resolve)));
    const doc = scriptedDoc();
    const source = new CloudDataSource({
      rest: { listMessages, postMessage } as never,
      openDoc: () => doc.handle,
      acquire: () => () => undefined,
      clientId: () => "web-1",
    });
    installChatRuntime({ source, host: {} as ChatHost });

    // The reader opens the chat and sends.
    const leave = useChatStore.getState().open(CHAT);
    useChatStore.getState().send(CHAT, SAID);
    // The box takes the turn and answers before the POST has come back.
    doc.push(appendOp("user", ECHO));
    doc.push(appendOp("assistant", ANSWER));
    answerSend(promptRow(SAID));
    await vi.waitFor(() => expect(userSaid().length).toBeGreaterThan(0));

    // Away to another chat, then back in — no reload, so the fold is the one
    // the live stream built.
    leave();
    const back = useChatStore.getState().open(CHAT);
    await vi.waitFor(() => expect(useChatStore.getState().byId[CHAT]?.loading).toBe(false));

    expect(userSaid(), "the message the reader sent is in the transcript once").toEqual([SAID]);
    expect(
      turnsNow().find((turn) => turn.author === "user")?.status,
      "a box that answered it took it — it is not still going out",
    ).not.toBe("running");
    expect(JSON.stringify(turnsNow())).toContain("I'm Claude Opus 5.");
    back();
  });
});

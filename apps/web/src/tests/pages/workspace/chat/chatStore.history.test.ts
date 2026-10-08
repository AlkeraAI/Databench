// Older turns joining above the transcript are not echoes of anything this
// tab sent: the optimistic bubble stays until the machine's own echo lands.

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ConversationTurn } from "@alkera/chat-model";

import { useChatStore } from "@/pages/workspace/chat/chatStore";
import type { ChatEvent } from "@/pages/workspace/chat/data/model";

const listeners = new Set<(event: ChatEvent) => void>();
let turns: ConversationTurn[] = [];

vi.mock("@/pages/workspace/chat/data", () => ({
  chatData: () => ({
    getChatTurns: async () => structuredClone(turns),
    subscribeChat: (_chatId: string, onEvent: (event: ChatEvent) => void) => {
      listeners.add(onEvent);
      return () => listeners.delete(onEvent);
    },
    sendUserMessage: async () => ({ id: "m", role: "user", content: "x" }),
  }),
}));

function user(id: string, text: string): ConversationTurn {
  return { id, author: "user", status: "done", parts: [{ id: `${id}-text`, kind: "text", text }] };
}

function announce(kind: ChatEvent["kind"]): void {
  for (const listener of listeners) listener({ id: kind, chatId: "c1", kind, content: "" });
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  listeners.clear();
  useChatStore.setState({ byId: {}, pending: {} });
});

describe("a page of history landing above", () => {
  it("retires no optimistic bubble, while an echo below still does", async () => {
    turns = [user("u9", "latest")];
    const close = useChatStore.getState().open("c1");
    await settle();
    useChatStore.getState().send("c1", "just sent");
    expect(useChatStore.getState().byId.c1.optimistic).toHaveLength(1);

    // Two older user turns join above: the bubble must stay.
    turns = [user("u7", "old"), user("u8", "older"), user("u9", "latest")];
    announce("history_loaded");
    await settle();
    expect(useChatStore.getState().byId.c1.optimistic).toHaveLength(1);
    expect(useChatStore.getState().byId.c1.base.map((turn) => turn.id)).toEqual(["u7", "u8", "u9"]);

    // The machine echoes the send: now the bubble retires.
    turns = [...turns, user("u10", "just sent")];
    announce("graph_changed");
    await settle();
    expect(useChatStore.getState().byId.c1.optimistic).toHaveLength(0);
    close();
  });

  it("does not light the working indicator", async () => {
    turns = [user("u9", "latest")];
    const close = useChatStore.getState().open("c1");
    await settle();
    turns = [user("u8", "older"), user("u9", "latest")];
    announce("history_loaded");
    await settle();
    expect(useChatStore.getState().byId.c1.sending).toBe(false);
    close();
  });
});

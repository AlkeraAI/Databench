// The message POST is the one place the linked node ids reach the server.
// Pinned on the wire: `attachments` is the key, it carries exactly the ids the
// send was given, and a send with none does not mention it at all.

import { describe, expect, it, vi } from "vitest";

import type { DocHandle } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

function build() {
  const posted: { chatId: string; body: Record<string, unknown> }[] = [];
  const rest = {
    postMessage: vi.fn(async (chatId: string, body: Record<string, unknown>) => {
      posted.push({ chatId, body });
      return { id: "m1", seq: 1, kind: "user_message", text: String(body.text), created_at: "" };
    }),
    createChat: vi.fn(async (title: string | null) => ({
      id: "chat-new",
      title,
      machine_status: "ready",
      machine_id: null,
    })),
  };
  const source = new CloudDataSource({
    rest: rest as never,
    openDoc: () => ({ subscribe: () => () => {}, close: () => {} }) as unknown as DocHandle<never>,
    acquire: () => () => {},
    clientId: () => "client-1",
  });
  return { source, posted };
}

describe("CloudDataSource message attachments", () => {
  it("puts the linked node ids on the message body as `attachments`", async () => {
    const { source, posted } = build();
    await source.sendUserMessage("c1", "what is in it", { attachments: ["node-a", "node-b"] });
    expect(posted).toEqual([
      {
        chatId: "c1",
        body: { text: "what is in it", client_id: "client-1", attachments: ["node-a", "node-b"] },
      },
    ]);
  });

  it("sends no `attachments` key at all when nothing is attached", async () => {
    const { source, posted } = build();
    await source.sendUserMessage("c1", "plain");
    await source.sendUserMessage("c1", "empty", { attachments: [] });
    expect(posted.map((call) => Object.keys(call.body).sort())).toEqual([
      ["client_id", "text"],
      ["client_id", "text"],
    ]);
  });

  it("a chat created from its first message carries that message's attachments", async () => {
    const { source, posted } = build();
    await source.createChat("look at this", { attachments: ["node-a"] });
    expect(posted).toEqual([
      {
        chatId: "chat-new",
        body: { text: "look at this", client_id: "client-1", attachments: ["node-a"] },
      },
    ]);
  });
});

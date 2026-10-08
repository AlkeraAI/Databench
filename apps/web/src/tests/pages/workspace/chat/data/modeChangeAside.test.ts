// A mode change is a row on the transcript, written by the server between a
// person's message and the agent's turn. It answers nothing: the message above
// it is still waiting for the agent, so a reader must keep showing it as such.
// The server reads the same rows the same way (`answers_a_waiting_message`).

import { describe, expect, it, vi } from "vitest";

import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

const CHAT = "c1";
const AT = "2026-09-30T09:14:00Z";

function promptRow(seq: number) {
  return {
    id: `row-${seq}`,
    chat_id: CHAT,
    seq,
    role: "user",
    kind: "prompt",
    event_id: "usr:web-1",
    payload: { kind: "prompt", text: "Run the report", client_id: "web-1", user_id: "u1", attachments: [] },
    created_at: AT,
  };
}

function systemRow(seq: number, eventId: string, event: Record<string, unknown>) {
  return {
    id: `row-${seq}`,
    chat_id: CHAT,
    seq,
    role: "system",
    kind: String(event.event_type),
    event_id: eventId,
    payload: { event_id: eventId, role: "system", kind: String(event.event_type), payload: event },
    created_at: AT,
  };
}

const MODE_CHANGE = {
  event_type: "mode.changed",
  mode: "bypass",
  previous_mode: "read_only",
  decided_by_name: "Dana Okafor",
  decided_via: "web",
};

function sourceOver(rows: Record<string, unknown>[]) {
  const listMessages = vi.fn(async () => ({
    items: rows,
    next_after_seq: rows.length ? (rows[rows.length - 1].seq as number) : 0,
    resync_from: null,
    prev_before: rows.length ? (rows[0].seq as number) : null,
    has_older: false,
  }));
  const rest = {
    listMessages,
    getChat: vi.fn(async () => ({ id: CHAT, title: "Hi", updated_at: AT, permission_mode: "bypass" })),
  };
  return new CloudDataSource({
    rest: rest as never,
    openDoc: () => {
      throw new Error("no live document in this test");
    },
    acquire: () => () => undefined,
    clientId: () => "web-2",
  });
}

async function turnsOf(rows: Record<string, unknown>[]) {
  return sourceOver(rows).getChatTurns(CHAT);
}

describe("a mode change between a message and the agent's turn", () => {
  it("is a card, and the message above it is still waiting", async () => {
    const turns = await turnsOf([promptRow(1), systemRow(2, "aside-mode-1", MODE_CHANGE)]);
    const asked = turns.find((turn) => turn.author === "user");
    expect(asked?.status).toBe("running");
    const card = turns.flatMap((turn) => turn.parts).find((part) => part.kind === "system");
    expect(card).toMatchObject({
      text: "Mode set to Bypass permissions",
      detail: "Read-only → Bypass permissions · Changed on web by Dana Okafor",
    });
  });

  it("while a row the machine wrote does say the message was taken", async () => {
    const turns = await turnsOf([
      promptRow(1),
      systemRow(2, "m1-created", { event_type: "message.created", message_id: "m1", role: "assistant" }),
    ]);
    expect(turns.find((turn) => turn.author === "user")?.status).toBe("done");
  });
});

describe("a model change written to the transcript", () => {
  it("is a card in the mode card's shape, and the message above it is still waiting", async () => {
    const turns = await turnsOf([
      promptRow(1),
      systemRow(2, "aside-model-1", {
        event_type: "model.changed",
        model_id: "claude-sonnet-5.5",
        display_name: "Claude Sonnet 5.5",
        effort: "high",
        previous_model_id: "claude-haiku-4.5",
        previous_display_name: "Claude Haiku 4.5",
        previous_effort: "high",
        decided_by_name: "Bob Editor",
        decided_via: "web",
      }),
    ]);
    expect(turns.find((turn) => turn.author === "user")?.status).toBe("running");
    const card = turns.flatMap((turn) => turn.parts).find((part) => part.kind === "system");
    expect(card).toMatchObject({
      text: "Model set to Claude Sonnet 5.5",
      detail: "Claude Haiku 4.5 → Claude Sonnet 5.5 · Changed on web by Bob Editor",
    });
  });

  it("naming no model is nothing", async () => {
    const turns = await turnsOf([systemRow(1, "aside-model-2", { event_type: "model.changed", model_id: "" })]);
    expect(turns.flatMap((turn) => turn.parts).filter((part) => part.kind === "system")).toEqual([]);
  });
});

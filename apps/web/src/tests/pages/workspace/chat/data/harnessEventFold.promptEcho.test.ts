// A person's message reaches a reader three ways — the send's relay, the row
// a page returns, and the box's echo of it — and whichever lands first, the
// turn must read the same: the row's own time, the box's ids for the message
// and its part, and `done` once a machine has written after it.

import { describe, expect, it } from "vitest";

import {
  createConversationFoldState,
  foldHarnessEvent,
  foldRelayedPrompt,
  promptsWereTaken,
} from "@/pages/workspace/chat/data/harnessEventFold";

const WORDS = "which prompts moved this week?";
const ROW_AT = "2026-09-21T11:08:08.487604+00:00";
const ECHO_AT = "2026-09-21T11:08:08.696303Z";

const created = {
  event_type: "message.created",
  message_id: "msg_1",
  role: "user",
  time: ECHO_AT,
};
const part = {
  event_type: "part.started",
  message_id: "msg_1",
  part_id: "prt_1",
  part_type: "text",
  initial: { id: "prt_1", type: "text", text: `files above\n\n${WORDS}` },
};

describe("a relayed prompt and the box's echo of it", () => {
  it("folds to the same turn whichever lands first", () => {
    const relayFirst = createConversationFoldState({ agentHost: "workspace" });
    foldRelayedPrompt(relayFirst, { id: "usr:c1", text: WORDS, at: ROW_AT });
    foldHarnessEvent(relayFirst, created);
    foldHarnessEvent(relayFirst, part);

    const echoFirst = createConversationFoldState({ agentHost: "workspace" });
    foldHarnessEvent(echoFirst, created);
    foldHarnessEvent(echoFirst, part);
    foldRelayedPrompt(echoFirst, { id: "usr:c1", text: WORDS, at: ROW_AT });

    for (const state of [relayFirst, echoFirst]) {
      expect(state.turns).toHaveLength(1);
      const [turn] = state.turns;
      expect(turn.id, "the box's id for the message").toBe("msg_1");
      expect(turn.author).toBe("user");
      expect(turn.startedAt, "the row's stamp, not the echo's").toBe(ROW_AT);
      expect(turn.parts.map((p) => p.id), "the box's id for the part").toEqual(["prt_1"]);
      expect(turn.parts[0]).toMatchObject({ kind: "text", text: WORDS });
    }
  });

  it("routes a later part of the echoed message onto the merged turn", () => {
    const state = createConversationFoldState({ agentHost: "workspace" });
    foldRelayedPrompt(state, { id: "usr:c1", text: WORDS, at: ROW_AT });
    foldHarnessEvent(state, created);
    foldHarnessEvent(state, part);
    foldHarnessEvent(state, {
      event_type: "part.created",
      part: { part_id: "prt_1", message_id: "msg_1", type: "text", text: `files above\n\n${WORDS}` },
    });
    expect(state.turns).toHaveLength(1);
    expect(state.turns[0].parts.map((p) => p.id)).toEqual(["prt_1"]);
  });
});

describe("a prompt the machine has written after", () => {
  it("reads as taken once any later row folds, without an answer opening", () => {
    const state = createConversationFoldState({ agentHost: "workspace" });
    foldRelayedPrompt(state, { id: "usr:c1", text: WORDS, at: ROW_AT });
    expect(state.turns[0].status).toBe("running");
    promptsWereTaken(state);
    expect(state.turns[0].status).toBe("done");
  });

  it("leaves a cancelled prompt cancelled", () => {
    const state = createConversationFoldState({ agentHost: "workspace" });
    foldRelayedPrompt(state, { id: "usr:c1", text: WORDS, at: ROW_AT });
    foldHarnessEvent(state, { event_type: "prompt.cancelled", message_id: "usr:c1" });
    promptsWereTaken(state);
    expect(state.turns[0].status).toBe("cancelled");
  });
});

describe("the box's bare announcement of a person's message", () => {
  it("opens no turn until a part lands, so a repeat of it is nothing", () => {
    const state = createConversationFoldState({ agentHost: "workspace" });
    foldHarnessEvent(state, created);
    expect(state.turns, "an announcement alone is not a message").toEqual([]);
    foldHarnessEvent(state, { ...created, time: "2026-09-21T11:18:19.606583Z" });
    expect(state.turns).toEqual([]);
    foldHarnessEvent(state, part);
    expect(state.turns).toHaveLength(1);
    expect(state.turns[0]).toMatchObject({ id: "msg_1", author: "user", startedAt: ECHO_AT });
  });

  it("re-announced from inside the answer, adds no empty message", () => {
    const state = createConversationFoldState({ agentHost: "workspace" });
    foldHarnessEvent(state, {
      event_type: "message.created",
      message_id: "msg_a",
      role: "assistant",
      time: ECHO_AT,
    });
    foldHarnessEvent(state, {
      event_type: "part.started",
      message_id: "msg_a",
      part_id: "prt_a",
      part_type: "text",
      initial: { id: "prt_a", type: "text", text: "On it." },
    });
    // The person's message this answers was recorded pages above; the box
    // repeats its announcement as the session moves on.
    foldHarnessEvent(state, { ...created, time: "2026-09-21T11:18:19.606583Z" });
    expect(state.turns.map((t) => [t.id, t.author])).toEqual([["msg_a", "assistant"]]);
  });
});

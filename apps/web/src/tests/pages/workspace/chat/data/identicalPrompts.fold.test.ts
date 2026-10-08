// Two sayings of the same words are two messages, and each has its own echo.
// The words cannot say which echo is whose; the transcript's order can. This
// is the push-5a window's rows 26/27..29 and 61/62..64 — the same 83
// characters said twice — folded in every order a cold open can meet them in:
// the socket window holds the box's echoes and no prompt rows, and the REST
// pages bring the prompt rows newest page first.

import { describe, expect, it } from "vitest";

import {
  cloneTurns,
  createConversationFoldState,
  foldHarnessEvent,
  foldRelayedPrompt,
  setFoldingSeq,
  type ConversationFoldState,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";

const WORDS = "Without using any tool, write 600 words about oceans, slowly and carefully.";
const FIRST = { id: "usr:web-0cb8b646-1", text: WORDS, at: "2026-09-21T16:12:05.897867Z", seq: 26 };
const SECOND = { id: "usr:web-3cc04e56-1", text: WORDS, at: "2026-09-21T16:13:12.044729Z", seq: 61 };

function echo(state: ConversationFoldState, messageId: string, firstSeq: number, time: string): void {
  const events: HarnessEvent[] = [
    { event_type: "message.created", message_id: messageId, role: "user", time },
    {
      event_type: "part.started",
      message_id: messageId,
      part_id: `${messageId}-reminder`,
      part_type: "text",
      initial: { id: `${messageId}-reminder`, type: "text", text: "<system-reminder>…", synthetic: true },
    },
    {
      event_type: "part.started",
      message_id: messageId,
      part_id: `${messageId}-words`,
      part_type: "text",
      initial: { id: `${messageId}-words`, type: "text", text: WORDS },
    },
  ];
  events.forEach((event, i) => {
    setFoldingSeq(state, firstSeq + i);
    foldHarnessEvent(state, event);
    setFoldingSeq(state, null);
  });
}

const ECHO_OF_FIRST = (state: ConversationFoldState) => echo(state, "msg_first", 27, "2026-09-21T16:12:06.1Z");
const ECHO_OF_SECOND = (state: ConversationFoldState) => echo(state, "msg_second", 62, "2026-09-21T16:13:12.2Z");
const SAY_FIRST = (state: ConversationFoldState) => foldRelayedPrompt(state, FIRST);
const SAY_SECOND = (state: ConversationFoldState) => foldRelayedPrompt(state, SECOND);

const EXPECTED = [
  ["msg_first", FIRST.at],
  ["msg_second", SECOND.at],
];

function dated(steps: Array<(state: ConversationFoldState) => void>) {
  const state = createConversationFoldState();
  for (const step of steps) step(state);
  return cloneTurns(state)
    .filter((turn) => turn.author === "user")
    .map((turn) => [turn.id, turn.startedAt])
    .sort((a, b) => String(a[0]).localeCompare(String(b[0])));
}

describe("two messages of the same words", () => {
  it.each([
    ["the log's own order", [SAY_FIRST, ECHO_OF_FIRST, SAY_SECOND, ECHO_OF_SECOND]],
    ["both echoes, then the newer page's prompt, then the older page's", [ECHO_OF_FIRST, ECHO_OF_SECOND, SAY_SECOND, SAY_FIRST]],
    ["both echoes, then the prompts oldest first", [ECHO_OF_FIRST, ECHO_OF_SECOND, SAY_FIRST, SAY_SECOND]],
    ["the second saying before the first one's echo", [SAY_FIRST, SAY_SECOND, ECHO_OF_FIRST, ECHO_OF_SECOND]],
    ["the newer page first, whole", [SAY_SECOND, ECHO_OF_SECOND, SAY_FIRST, ECHO_OF_FIRST]],
  ])("each echo is dated by its own saying — %s", (_order, steps) => {
    expect(dated(steps)).toEqual(EXPECTED);
  });

  it("an echo the box wrote before a message existed is never that message's", () => {
    // Only the first echo stands, and only the second saying's row has been
    // read: the row waits for its own echo rather than taking the older one.
    const state = createConversationFoldState();
    ECHO_OF_FIRST(state);
    SAY_SECOND(state);
    const turns = cloneTurns(state).filter((turn) => turn.author === "user");
    expect(turns.map((turn) => turn.id)).toEqual(["msg_first", SECOND.id]);
    expect(turns.find((turn) => turn.id === "msg_first")?.startedAt).not.toBe(SECOND.at);
  });

  it("a source with no sequences still pairs them in the order they stand", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "m1", role: "user", time: "t1" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "m1",
      part: { part_id: "m1-p", message_id: "m1", type: "text", text: WORDS },
    });
    foldRelayedPrompt(state, { id: "usr:a", text: WORDS, at: "said" });
    const turns = cloneTurns(state).filter((turn) => turn.author === "user");
    expect(turns.map((turn) => [turn.id, turn.startedAt])).toEqual([["m1", "said"]]);
  });
});

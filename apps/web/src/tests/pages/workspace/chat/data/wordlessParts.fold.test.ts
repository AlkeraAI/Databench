// A text or thought the box never wrote down: its words reached the tab as
// token frames and no `part.created` ever recorded them. Whatever ends the
// message — the next message, the turn's terminal, or the ordinary
// `message.completed` and `idle` — a tab that watched must show what a reload
// shows, and a reload holds nothing of it. The record's own row, whenever it
// lands, is what puts the words back.

import { describe, expect, it } from "vitest";

import {
  cloneTurns,
  createConversationFoldState,
  foldHarnessEvent,
  type ConversationFoldState,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";

const MSG = "msg_answer";
const PART = "prt_prose";

const opened: HarnessEvent = { event_type: "message.created", message_id: MSG, role: "assistant" };
const started: HarnessEvent = {
  event_type: "part.started",
  message_id: MSG,
  part_id: PART,
  part_type: "text",
  initial: { id: PART, messageID: MSG, type: "text", text: "" },
};
const token: HarnessEvent = { event_type: "agent.message_chunk", message_id: MSG, part_id: PART, text: "Granite is" };
const recorded: HarnessEvent = {
  event_type: "part.created",
  part: { part_id: PART, message_id: MSG, type: "text", text: "Granite is a coarse-grained rock." },
};
const completed: HarnessEvent = { event_type: "message.completed", message_id: MSG };
const idle: HarnessEvent = { event_type: "session.status_changed", status: "idle" };
const abortedWithDetail: HarnessEvent = {
  event_type: "session.status_changed",
  status: "aborted",
  detail: "The workspace restarted while it was answering.",
};
const nextMessage: HarnessEvent = { event_type: "message.created", message_id: "msg_next", role: "assistant" };
const nextPart: HarnessEvent = {
  event_type: "part.started",
  message_id: "msg_next",
  part_id: "prt_next",
  part_type: "text",
  initial: { id: "prt_next", messageID: "msg_next", type: "text", text: "" },
};

/** The tab that watched: token frames included. */
function live(events: HarnessEvent[]): ConversationFoldState {
  const state = createConversationFoldState();
  for (const event of events) foldHarnessEvent(state, event);
  return state;
}

/** A reload: the same events with the token frames left out, which is all the
 *  record holds. */
const reload = (events: HarnessEvent[]) => live(events.filter((event) => event.event_type !== "agent.message_chunk"));

const partsOf = (state: ConversationFoldState) =>
  cloneTurns(state)
    .find((turn) => turn.id === MSG)
    ?.parts.map((part) => ("text" in part ? `${part.kind}:${part.text}` : part.kind));

describe("a part the box never wrote down", () => {
  it.each([
    ["opened by its row, then the ordinary end", [opened, started, token, completed, idle]],
    ["opened by a token frame alone, then the ordinary end", [opened, token, completed, idle]],
    ["still open when the turn ends on a detail", [opened, started, token, abortedWithDetail]],
    ["still open when the box moves to the next message", [opened, started, token, nextMessage, nextPart]],
  ])("is gone live as it is after a reload — %s", (_how, events) => {
    expect(partsOf(live(events))).toEqual([]);
    expect(partsOf(reload(events))).toEqual([]);
  });

  it("is kept, with the record's words, when its row lands before the end", () => {
    const events = [opened, started, token, recorded, completed, idle];
    expect(partsOf(live(events))).toEqual(["text:Granite is a coarse-grained rock."]);
    expect(partsOf(reload(events))).toEqual(partsOf(live(events)));
  });

  it("comes back in its place when its row lands late, after the box moved on", () => {
    const events = [opened, started, token, nextMessage, nextPart, recorded];
    const state = live(events);
    expect(partsOf(state)).toEqual(["text:Granite is a coarse-grained rock."]);
    // In its own message, not under the one that closed it.
    const next = cloneTurns(state).find((turn) => turn.id === "msg_next");
    expect(next?.parts.some((part) => part.id === PART)).toBe(false);
    expect(partsOf(reload(events))).toEqual(partsOf(state));
  });

  it("is dropped once the ordinary end has passed, but not one that was only ever streaming into a sibling", () => {
    // The ordinary end: `message.completed` alone is what settles this message,
    // whether or not `idle` has landed yet.
    expect(partsOf(live([opened, started, token, completed]))).toEqual([]);
    // A sibling's opening in the same message leaves it standing: the
    // coalescing lane may still bring its row.
    const sibling: HarnessEvent = {
      event_type: "part.started",
      message_id: MSG,
      part_id: "prt_sibling",
      part_type: "text",
      initial: { id: "prt_sibling", messageID: MSG, type: "text", text: "" },
    };
    expect(partsOf(live([opened, started, token, sibling]))).toEqual(["text:Granite is"]);
  });
});

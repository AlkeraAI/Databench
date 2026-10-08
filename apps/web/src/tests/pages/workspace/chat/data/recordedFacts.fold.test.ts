// Rows the server writes on a member's behalf reach a live tab in more than
// one telling: the relay the server sends the box, the box's own resolution,
// and — read back — the recorded row that names the member. The fold settles
// on the first telling and lets a later one add what it knows, never take
// away. The events are in the order a recorded window holds them.

import { describe, expect, it } from "vitest";

import {
  A_MEMBER_OF_THIS_WORKSPACE,
  cloneTurns,
  createConversationFoldState,
  foldHarnessEvent,
  foldRelayedAnswer,
  foldRelayedPrompt,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";

const REQUEST = "2a8a06fb8ac1255670a1";

const ASK: HarnessEvent = {
  event_type: "permission.request",
  message_id: "msg-a1",
  request_id: REQUEST,
  permission_kind: "shell",
  patterns: ["uv run python -c 'print(7)'"],
  prompting: true,
  options: [
    { option_id: "allow_once", name: "Allow once" },
    { option_id: "reject_once", name: "Reject once" },
  ],
};
const NAMED: HarnessEvent = {
  event_type: "permission.resolved",
  request_id: REQUEST,
  option_id: "allow_once",
  decided_by: "user",
  decided_by_name: "Qa Viewer",
};
const NAMELESS: HarnessEvent = {
  event_type: "permission.resolved",
  request_id: REQUEST,
  option_id: "allow_once",
  decided_by: "user",
};
const POLICY: HarnessEvent = {
  event_type: "permission.resolved",
  request_id: REQUEST,
  option_id: "allow_once",
  decided_by: "policy",
};

function fold(events: HarnessEvent[]) {
  const state = createConversationFoldState();
  foldHarnessEvent(state, { event_type: "message.created", message_id: "msg-a1", role: "assistant" });
  for (const event of events) foldHarnessEvent(state, event);
  return state;
}

function askOf(state: ReturnType<typeof fold>) {
  for (const turn of cloneTurns(state)) {
    for (const part of turn.parts) {
      if (part.kind === "permission" && part.requestId === REQUEST) return part;
    }
  }
  return null;
}

describe("an ask resolved more than once on the wire", () => {
  it.each([
    ["named, then the box's nameless one", [NAMED, NAMELESS]],
    ["nameless, then the server's named one", [NAMELESS, NAMED]],
  ])("keeps the member's name — %s", (_order, tellings) => {
    const state = fold([ASK, ...tellings]);
    expect(askOf(state)).toMatchObject({
      status: "resolved",
      selectedOptionId: "allow_once",
      decidedByName: "Qa Viewer",
    });
  });

  it.each([
    ["named, then nameless", [NAMED, NAMELESS]],
    ["nameless, then named", [NAMELESS, NAMED]],
  ])("keeps the name when the ask lands after both — %s", (_order, tellings) => {
    const state = fold([...tellings, ASK]);
    expect(askOf(state)).toMatchObject({
      status: "resolved",
      selectedOptionId: "allow_once",
      decidedByName: "Qa Viewer",
    });
  });

  it("the first decision stands: a later telling never rewrites the option", () => {
    const state = fold([ASK, NAMED, { ...NAMELESS, option_id: "reject_once" }]);
    expect(askOf(state)).toMatchObject({ selectedOptionId: "allow_once", decidedByName: "Qa Viewer" });
  });

  it("a member's decision the wire does not name is still a member's", () => {
    const state = fold([ASK, NAMELESS]);
    expect(askOf(state)).toMatchObject({
      status: "resolved",
      selectedOptionId: "allow_once",
      decidedByName: A_MEMBER_OF_THIS_WORKSPACE,
    });
  });

  it("a policy's decision names nobody, and stays nobody's", () => {
    expect(askOf(fold([ASK, POLICY]))).toMatchObject({ status: "resolved" });
    expect(askOf(fold([ASK, POLICY]))?.decidedByName).toBeUndefined();
  });

  it("the neutral name gives way to the member's, never the reverse", () => {
    expect(askOf(fold([ASK, NAMELESS, NAMED]))?.decidedByName).toBe("Qa Viewer");
    expect(askOf(fold([ASK, NAMED, NAMELESS]))?.decidedByName).toBe("Qa Viewer");
  });
});

describe("a member's answer relayed before any resolution", () => {
  it("settles the card in place for every reader, on the option", () => {
    const state = fold([ASK]);
    foldRelayedAnswer(state, { requestId: REQUEST, optionId: "allow_once" });
    expect(askOf(state)).toMatchObject({
      status: "resolved",
      selectedOptionId: "allow_once",
      decidedByName: A_MEMBER_OF_THIS_WORKSPACE,
    });
    // The record, read back, names the member; the box's telling adds nothing.
    foldHarnessEvent(state, NAMED);
    foldHarnessEvent(state, NAMELESS);
    expect(askOf(state)?.decidedByName).toBe("Qa Viewer");
  });

  it("names nothing it was not told: a relay without an option settles nothing", () => {
    const state = fold([ASK]);
    foldRelayedAnswer(state, { requestId: REQUEST });
    expect(askOf(state)?.status).toBe("pending");
  });

  it("is kept for an ask that lands after it", () => {
    const state = fold([]);
    foldRelayedAnswer(state, { requestId: REQUEST, optionId: "allow_once" });
    foldHarnessEvent(state, ASK);
    expect(askOf(state)).toMatchObject({
      status: "resolved",
      selectedOptionId: "allow_once",
      decidedByName: A_MEMBER_OF_THIS_WORKSPACE,
    });
  });
});

describe("a Stop note delivered as a started and created pair", () => {
  const NOTE = "stop-15ab78c707a2";
  const part = { part_id: `${NOTE}-part`, message_id: NOTE, type: "text", text: "Stopped by Member One.", synthetic: true };
  const started: HarnessEvent = {
    event_type: "part.started",
    message_id: NOTE,
    part_id: part.part_id,
    part_type: "text",
    initial: { id: part.part_id, messageID: NOTE, ...part },
  };
  const created: HarnessEvent = { event_type: "part.created", part };
  const row: HarnessEvent = { event_type: "message.created", message_id: NOTE, role: "system" };
  const done: HarnessEvent = { event_type: "message.completed", message_id: NOTE };

  it.each([
    ["row first", [row, started, created, done]],
    ["row late", [started, created, row, done]],
    ["row never, turn ends", [started, created, { event_type: "session.status_changed", status: "aborted", detail: null }]],
  ])("renders the sentence, never an empty block — %s", (_order, events) => {
    const state = createConversationFoldState();
    for (const event of events) foldHarnessEvent(state, event);
    const turn = cloneTurns(state).find((t) => t.id === NOTE);
    expect(turn?.author).toBe("system");
    expect(turn?.parts).toEqual([
      expect.objectContaining({ kind: "system", text: "Stopped by Member One." }),
    ]);
  });
});

describe("a person who says the same words twice", () => {
  // Recorded rows 1, 5, 8, 168 and 172: the first saying (1) was stopped
  // before any box took it, the box then took a different message (5, echoed
  // at 8), and its echo at 172 answers the second saying (168). The echoed
  // turn is dated by the second saying — what a page that never held the
  // first one shows — because the box passed the first one over.
  const WORDS = "Run this exact bash command in the foreground.";
  const echo = (messageId: string, text: string, time: string): HarnessEvent[] => [
    { event_type: "message.created", message_id: messageId, role: "user", time },
    {
      event_type: "part.created",
      message_id: messageId,
      part: { part_id: `${messageId}-prt`, message_id: messageId, type: "text", text },
    },
  ];

  it("dates the echoed turn by the saying the box took, not the one it passed over", () => {
    const state = createConversationFoldState();
    foldRelayedPrompt(state, { id: "usr:web-1", text: WORDS, at: "2026-09-21T15:15:52Z" });
    foldRelayedPrompt(state, { id: "usr:web-5", text: "Something else.", at: "2026-09-21T15:16:20Z" });
    for (const event of echo("msg-8", "Something else.", "2026-09-21T15:16:26Z")) foldHarnessEvent(state, event);
    foldRelayedPrompt(state, { id: "usr:web-168", text: WORDS, at: "2026-09-21T15:23:59Z" });
    for (const event of echo("msg-172", WORDS, "2026-09-21T15:24:04Z")) foldHarnessEvent(state, event);
    const turns = cloneTurns(state).filter((t) => t.author === "user");
    expect(turns.map((t) => [t.id, t.startedAt])).toEqual([
      ["usr:web-1", "2026-09-21T15:15:52Z"],
      ["msg-8", "2026-09-21T15:16:20Z"],
      ["msg-172", "2026-09-21T15:23:59Z"],
    ]);
  });

  it("still answers the first of two sayings when the box took neither yet", () => {
    const state = createConversationFoldState();
    foldRelayedPrompt(state, { id: "usr:web-1", text: WORDS, at: "2026-09-21T15:15:52Z" });
    foldRelayedPrompt(state, { id: "usr:web-2", text: WORDS, at: "2026-09-21T15:15:58Z" });
    for (const event of echo("msg-3", WORDS, "2026-09-21T15:16:00Z")) foldHarnessEvent(state, event);
    const turns = cloneTurns(state).filter((t) => t.author === "user");
    expect(turns.map((t) => [t.id, t.startedAt])).toEqual([
      ["msg-3", "2026-09-21T15:15:52Z"],
      ["usr:web-2", "2026-09-21T15:15:58Z"],
    ]);
  });
});

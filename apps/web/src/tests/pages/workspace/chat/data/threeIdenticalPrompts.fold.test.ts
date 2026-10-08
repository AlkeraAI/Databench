// Three sayings of the same words, three echoes, every order a tab can meet
// them in. The socket window holds the echoes in the transcript's order and no
// prompt rows; the pages bring the prompt rows in any order (newest page
// first, older pages later, a forward walk after that) — so the three sayings
// arrive in any of their six orders, woven any way through the three echoes.
// 120 arrivals; in each, every echo is dated by its own saying.

import { describe, expect, it } from "vitest";

import {
  cloneTurns,
  createConversationFoldState,
  foldHarnessEvent,
  foldRelayedPrompt,
  setFoldingSeq,
  type ConversationFoldState,
} from "@/pages/workspace/chat/data/harnessEventFold";

const WORDS = "Without using any tool, write 600 words about volcanoes.";
const SAYINGS = [
  { id: "usr:web-a", text: WORDS, at: "2026-09-21T16:12:05Z", seq: 26, echo: "msg_a", echoSeq: 27 },
  { id: "usr:web-b", text: WORDS, at: "2026-09-21T16:13:12Z", seq: 61, echo: "msg_b", echoSeq: 62 },
  { id: "usr:web-c", text: WORDS, at: "2026-09-21T16:14:40Z", seq: 95, echo: "msg_c", echoSeq: 96 },
];

type Step = { label: string; run: (state: ConversationFoldState) => void };

const say = (i: number): Step => ({
  label: `S${i + 1}`,
  run: (state) => foldRelayedPrompt(state, SAYINGS[i]),
});

const echo = (i: number): Step => ({
  label: `E${i + 1}`,
  run: (state) => {
    const { echo: messageId, echoSeq } = SAYINGS[i];
    setFoldingSeq(state, echoSeq);
    foldHarnessEvent(state, { event_type: "message.created", message_id: messageId, role: "user", time: `echo-${i}` });
    setFoldingSeq(state, echoSeq + 2);
    foldHarnessEvent(state, {
      event_type: "part.started",
      message_id: messageId,
      part_id: `${messageId}-words`,
      part_type: "text",
      initial: { id: `${messageId}-words`, type: "text", text: WORDS },
    });
    setFoldingSeq(state, null);
  },
});

function permutations<T>(items: T[]): T[][] {
  if (items.length <= 1) return [items];
  return items.flatMap((item, i) =>
    permutations([...items.slice(0, i), ...items.slice(i + 1)]).map((rest) => [item, ...rest]),
  );
}

/** Every way of weaving `a` and `b`, each keeping its own order. */
function weaves<T>(a: T[], b: T[]): T[][] {
  if (a.length === 0) return [b];
  if (b.length === 0) return [a];
  return [
    ...weaves(a.slice(1), b).map((rest) => [a[0], ...rest]),
    ...weaves(a, b.slice(1)).map((rest) => [b[0], ...rest]),
  ];
}

const ECHOES = [echo(0), echo(1), echo(2)];
const ARRIVALS = permutations([say(0), say(1), say(2)]).flatMap((sayings) => weaves(sayings, ECHOES));

describe("three messages of the same words", () => {
  it("covers every arrival", () => {
    expect(ARRIVALS).toHaveLength(120);
  });

  it.each(ARRIVALS.map((steps) => [steps.map((step) => step.label).join(" "), steps] as const))(
    "each echo is dated by its own saying — %s",
    (_order, steps) => {
      const state = createConversationFoldState();
      for (const step of steps) step.run(state);
      const dated = Object.fromEntries(
        cloneTurns(state)
          .filter((turn) => turn.author === "user")
          .map((turn) => [turn.id, turn.startedAt]),
      );
      expect(dated).toEqual({
        msg_a: SAYINGS[0].at,
        msg_b: SAYINGS[1].at,
        msg_c: SAYINGS[2].at,
      });
    },
  );
});

describe("two sayings queued before the first echo", () => {
  // Both messages were recorded (10, 11) before the box echoed either (12,
  // 40), so neither is ruled out by when it was said: what picks the first
  // echo's saying is that the box takes messages in the transcript's order —
  // not the order the pages delivered them in, which is newest first.
  const QUEUED = [
    { id: "usr:web-q1", text: WORDS, at: "2026-09-21T16:00:10Z", seq: 10, echo: "msg_q1", echoSeq: 12 },
    { id: "usr:web-q2", text: WORDS, at: "2026-09-21T16:00:11Z", seq: 11, echo: "msg_q2", echoSeq: 40 },
  ];
  const sayQ = (i: number) => (state: ConversationFoldState) => foldRelayedPrompt(state, QUEUED[i]);
  const echoQ = (i: number) => (state: ConversationFoldState) => {
    const { echo: messageId, echoSeq } = QUEUED[i];
    setFoldingSeq(state, echoSeq);
    foldHarnessEvent(state, { event_type: "message.created", message_id: messageId, role: "user", time: `echo-q${i}` });
    foldHarnessEvent(state, {
      event_type: "part.started",
      message_id: messageId,
      part_id: `${messageId}-words`,
      part_type: "text",
      initial: { id: `${messageId}-words`, type: "text", text: WORDS },
    });
    setFoldingSeq(state, null);
  };

  it.each([
    ["the sayings newest first", [sayQ(1), sayQ(0), echoQ(0), echoQ(1)]],
    ["the sayings oldest first", [sayQ(0), sayQ(1), echoQ(0), echoQ(1)]],
    ["the newer saying, the first echo's saying only after it", [sayQ(1), sayQ(0), echoQ(0)]],
  ])("the first echo is the first saying's — %s", (_order, steps) => {
    const state = createConversationFoldState();
    for (const step of steps) step(state);
    const dated = Object.fromEntries(
      cloneTurns(state)
        .filter((turn) => turn.author === "user")
        .map((turn) => [turn.id, turn.startedAt]),
    );
    expect(dated.msg_q1).toBe(QUEUED[0].at);
    if (steps.length === 4) expect(dated.msg_q2).toBe(QUEUED[1].at);
    else expect(dated["usr:web-q2"]).toBe(QUEUED[1].at);
  });
});

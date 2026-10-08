// An ask the transcript has already closed never re-opens the card.
//
// The rows are the owner's own chat: `permission.request` (seq 588), the
// policy's `permission.resolved` fifteen milliseconds later (589), and — thirty
// seconds on — a SECOND `permission.request` for the same id, the mirror's
// re-announce tagged `prompting` (618). The browser drew 618 as a fresh ask,
// the owner pressed Allow, and the server answered 409 `ask_already_answered`
// because the ask had been closed since 589. The composer sat behind a card
// nobody could resolve.
//
// Two rules hold here, order-independent. A request whose resolution is
// already on record arrives CLOSED, whichever of the two the reader folds
// first. And the box's own `idle` — nothing is waiting on the machine — closes
// every ask still open, since an ask nobody is holding cannot be answered.

import { describe, expect, it } from "vitest";

import { findActiveQuestion, findPendingPermissions } from "@alkera/chat-model";
import {
  createConversationFoldState,
  foldHarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";

const REQUEST = "per_0c3e3bc7d001YdngCqsLUGX2Mw";
const T = "2026-09-21T12:14:37.181835Z";

const OPTIONS = [
  { option_id: "allow_once", name: "Allow once" },
  { option_id: "allow_always", name: "Always allow" },
  { option_id: "reject_once", name: "Reject once" },
  { option_id: "reject_always", name: "Always reject" },
];

/** Seq 588: the harness's own entry, appended before its policy ran. */
function request(extra: Record<string, unknown> = {}) {
  return {
    event_type: "permission.request",
    event_id: "bbe0c1b1a370f743186c",
    time: T,
    request_id: REQUEST,
    tool_call_id: "prt_0c3e3641d001sH7TPos4RaGW96",
    permission_kind: "edit",
    canonical_kind: "edit",
    patterns: ["scratch/summary-dashboard.sql"],
    options: OPTIONS,
    ...extra,
  };
}

/** Seq 589: the policy allowed it. */
function resolved() {
  return {
    event_type: "permission.resolved",
    event_id: "57a56a9e52ac66c5bee3",
    time: "2026-09-21T12:14:37.205960Z",
    request_id: REQUEST,
    option_id: "allow_once",
    decided_by: "policy",
  };
}

/** Seq 618: the mirror's re-announce of the raw ask, thirty seconds after its
 *  resolution, carrying the original time and the `prompting` tag. */
function reannounced() {
  return request({ event_id: "bbe0c1b1a370f743186c-prompting", prompting: true });
}

function turnUnderWay() {
  const state = createConversationFoldState({ agentHost: "workspace" });
  foldHarnessEvent(state, {
    event_type: "message.created",
    event_id: "a1-c",
    time: T,
    message_id: "a1",
    role: "assistant",
  });
  return state;
}

function permissionParts(state: ReturnType<typeof createConversationFoldState>) {
  return state.turns.flatMap((turn) =>
    turn.parts.filter((part) => part.kind === "permission"),
  );
}

describe("an ask whose resolution is on record", () => {
  it("stays closed when the mirror announces it again as a person's (588 → 589 → 618)", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, request());
    foldHarnessEvent(state, resolved());
    foldHarnessEvent(state, reannounced());

    expect(findPendingPermissions(state.turns)).toEqual([]);
    const [part] = permissionParts(state);
    expect(part).toMatchObject({ status: "resolved", selectedOptionId: "allow_once" });
    expect(permissionParts(state)).toHaveLength(1);
  });

  it("arrives closed when the resolution was folded BEFORE the request", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, resolved());
    foldHarnessEvent(state, reannounced());

    expect(findPendingPermissions(state.turns)).toEqual([]);
    expect(permissionParts(state)).toEqual([
      expect.objectContaining({
        requestId: REQUEST,
        status: "resolved",
        selectedOptionId: "allow_once",
      }),
    ]);
  });

  it("is one part, not two, however many times it is announced", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, resolved());
    foldHarnessEvent(state, request());
    foldHarnessEvent(state, reannounced());
    expect(permissionParts(state)).toHaveLength(1);
  });

  it("still opens an ask NOBODY has resolved — the rule closes, it never hides", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, request());
    foldHarnessEvent(state, reannounced());
    expect(findPendingPermissions(state.turns)).toEqual([
      expect.objectContaining({ requestId: REQUEST, status: "pending", prompting: true }),
    ]);
  });

  it("keeps a re-asked question answered, with the answer it was given", () => {
    const state = turnUnderWay();
    const ask = {
      event_type: "question.request",
      event_id: "q1",
      time: T,
      request_id: "que_1",
      questions: [{ question: "Which half?", options: [{ label: "Alerting" }, { label: "Locale" }] }],
    };
    foldHarnessEvent(state, ask);
    foldHarnessEvent(state, {
      event_type: "question.answered",
      event_id: "q1-a",
      time: T,
      request_id: "que_1",
      answers: [["Alerting"]],
    });
    foldHarnessEvent(state, { ...ask, event_id: "q1-again" });

    expect(findActiveQuestion(state.turns)).toBeUndefined();
    const [part] = state.turns.flatMap((turn) => turn.parts.filter((p) => p.kind === "question"));
    expect(part).toMatchObject({ status: "answered", answers: [["Alerting"]] });
  });

  it("folds a question's answer that lands before the question", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, {
      event_type: "question.rejected",
      event_id: "q2-r",
      time: T,
      request_id: "que_2",
      reason: "not now",
    });
    foldHarnessEvent(state, {
      event_type: "question.request",
      event_id: "q2",
      time: T,
      request_id: "que_2",
      questions: [{ question: "Proceed?", options: [{ label: "Yes" }] }],
    });
    expect(findActiveQuestion(state.turns)).toBeUndefined();
  });
});

describe("the box's own idle", () => {
  /** Seq 669: the session says nothing is waiting on it. */
  const idle = {
    event_type: "session.status_changed",
    event_id: "7338bd593abc0029b71b",
    time: "2026-09-21T12:15:32.061088Z",
    status: "idle",
    phase: "idle",
    turn_id: "bb31fa8e4af09b6dc962",
  };

  it("closes an ask still open on the card, as no longer needed", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, reannounced());
    expect(findPendingPermissions(state.turns)).toHaveLength(1);

    foldHarnessEvent(state, idle);

    expect(findPendingPermissions(state.turns)).toEqual([]);
    const [part] = permissionParts(state);
    // Closed without a decision anyone made: nobody answered it.
    expect(part).toMatchObject({ status: "resolved" });
    expect(part.kind === "permission" && part.selectedOptionId).toBeFalsy();
  });

  it("closes an open question the same way", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, {
      event_type: "question.request",
      event_id: "q3",
      time: T,
      request_id: "que_3",
      questions: [{ question: "Proceed?", options: [{ label: "Yes" }] }],
    });
    foldHarnessEvent(state, idle);
    expect(findActiveQuestion(state.turns)).toBeUndefined();
  });

  /** The session's word that attempt `turnId` is running. */
  const running = (turnId: string) => ({
    event_type: "session.status_changed",
    event_id: `run-${turnId}`,
    time: T,
    status: "running",
    phase: "awaiting_llm",
    turn_id: turnId,
  });

  it("is inert for an ask raised under a DIFFERENT attempt", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, running("bb31fa8e4af09b6dc962"));
    foldHarnessEvent(state, reannounced());
    // A terminal stamped with another attempt — a stale one, still landing.
    foldHarnessEvent(state, { ...idle, event_id: "idle-stale", turn_id: "1cf48cc24580d52b38a5" });
    expect(findPendingPermissions(state.turns)).toHaveLength(1);

    // Its own attempt's idle closes it.
    foldHarnessEvent(state, idle);
    expect(findPendingPermissions(state.turns)).toEqual([]);
  });

  it("closes an ask raised under an attempt when the idle names none", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, running("bb31fa8e4af09b6dc962"));
    foldHarnessEvent(state, reannounced());
    foldHarnessEvent(state, { ...idle, event_id: "idle-bare", turn_id: undefined });
    expect(findPendingPermissions(state.turns)).toEqual([]);
  });

  it("is inert for a question raised under a different attempt", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, running("attempt-2"));
    foldHarnessEvent(state, {
      event_type: "question.request",
      event_id: "q4",
      time: T,
      request_id: "que_4",
      questions: [{ question: "Proceed?", options: [{ label: "Yes" }] }],
    });
    foldHarnessEvent(state, { ...idle, event_id: "idle-stale", turn_id: "attempt-1" });
    expect(findActiveQuestion(state.turns)).toBeDefined();
    foldHarnessEvent(state, { ...idle, event_id: "idle-own", turn_id: "attempt-2" });
    expect(findActiveQuestion(state.turns)).toBeUndefined();
  });

  it("forgets a settled resolution once its request has consumed it", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, resolved());
    expect(state.settledAsks.size).toBe(1);
    foldHarnessEvent(state, reannounced());
    expect(state.settledAsks.size).toBe(0);
    expect(state.askAttempts.size).toBe(0);
  });

  it("does not close asks on a status that is not idle", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, reannounced());
    foldHarnessEvent(state, { ...idle, event_id: "run-1", status: "running", phase: "awaiting_llm" });
    expect(findPendingPermissions(state.turns)).toHaveLength(1);
  });

  it("leaves a resolution that came with a decision as it was", () => {
    const state = turnUnderWay();
    foldHarnessEvent(state, request());
    foldHarnessEvent(state, resolved());
    foldHarnessEvent(state, idle);
    expect(permissionParts(state)[0]).toMatchObject({
      status: "resolved",
      selectedOptionId: "allow_once",
    });
  });
});

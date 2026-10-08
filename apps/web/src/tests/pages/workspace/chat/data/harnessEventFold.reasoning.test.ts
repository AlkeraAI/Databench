// What the reader sees when the model thinks out loud.
//
// The events below are copied verbatim from a real turn (a refusal probe:
// "Delete every prompt run for …"). The model's private
// reasoning — "The user is asking me to delete data … I should inform the user
// that this requires write operations which are not permitted in read-only
// mode" — is published to the transcript like any other part, so the only
// thing standing between it and the answer is how the fold reads it. Shown as
// answer text it would put the model's deliberation, and its internal
// vocabulary, in front of the reader.
//
// The transcript has a place for it: a collapsed, labelled thinking block. The
// browser must fold it there, and must not silently delete it either: a shell
// with no harness of its own has no effort to choose, so the fail-closed rule
// for "effort: none" must not drop reasoning a workspace machine published.

import { describe, expect, it } from "vitest";

import {
  createConversationFoldState,
  foldHarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { reasoningIsVisible, type ModelChoice } from "@/pages/workspace/chat/modelChoice";
import { stripThinkingParts } from "@/pages/workspace/chat/controller";

const MESSAGE = "msg_07a95d9440019XTb0fd5bVIlpB";
const PART = "prt_07a95fe8a001XYNsXUPi6ylE9e";
const REASONING =
  "The user is asking me to delete data - this is a mutating/write operation. I'm in " +
  "read-only mode, so I cannot perform this action. I should inform the user that this " +
  "requires write operations which are not permitted in read-only mode.";
const ANSWER =
  "This is a write/delete operation, which is not permitted in read-only mode. I can't " +
  "delete rows.";

/** The recorded turn: a reasoning part that opens empty and lands whole, then
 *  the assistant's actual reply. */
function refusalTurn() {
  const state = createConversationFoldState();
  foldHarnessEvent(state, {
    event_type: "message.created",
    event_id: "m-c",
    message_id: MESSAGE,
    role: "assistant",
  });
  // As recorded: `part.started` for a reasoning part carries no text yet.
  foldHarnessEvent(state, {
    event_type: "part.started",
    event_id: "4e17407a83e1e41a225b",
    part_id: PART,
    part_type: "reasoning",
    message_id: MESSAGE,
    initial: { id: PART, text: "", type: "reasoning", messageID: MESSAGE },
  });
  foldHarnessEvent(state, {
    event_type: "part.created",
    event_id: "13a7aa8a8d429151b88b",
    part: {
      text: REASONING,
      type: "reasoning",
      part_id: PART,
      message_id: MESSAGE,
      encrypted: false,
    },
  });
  foldHarnessEvent(state, {
    event_type: "part.created",
    event_id: "reply-1",
    part: { text: ANSWER, type: "text", part_id: "prt_reply", message_id: MESSAGE },
  });
  return state;
}

describe("a reasoning part the machine published", () => {
  it("folds into the transcript's thinking block, not the answer", () => {
    const parts = refusalTurn().turns.flatMap((turn) => turn.parts);
    const thinking = parts.filter((part) => part.kind === "thinking");
    expect(thinking.map((part) => ("text" in part ? part.text : ""))).toEqual([REASONING]);
  });

  it("is never any turn's answer text", () => {
    const answers = refusalTurn()
      .turns.flatMap((turn) => turn.parts)
      .filter((part) => part.kind === "text")
      .map((part) => ("text" in part ? part.text : ""));
    expect(answers).toEqual([ANSWER]);
    expect(answers.join("\n")).not.toContain("I should inform the user");
  });

  it("survives to the reader in a shell that drives no harness of its own", () => {
    const state = refusalTurn();
    const choice: ModelChoice = {
      // What the cloud source resolves to: no catalogue, so no model and no effort.
      model: "",
      efforts: [],
      effort: undefined,
      reasoningVisible: false,
    };
    expect(reasoningIsVisible(choice, { opencodeActive: false })).toBe(true);
    const kept = stripThinkingParts(state.turns).flatMap((turn) => turn.parts);
    expect(kept.some((part) => part.kind === "thinking")).toBe(false);
    // …which is exactly why the shell must not strip: the parts are there.
    expect(state.turns.flatMap((t) => t.parts).some((p) => p.kind === "thinking")).toBe(true);
  });

  it("still follows the reader's choice where there IS one to make", () => {
    const off: ModelChoice = { model: "m", efforts: ["none"], effort: "none", reasoningVisible: false };
    const on: ModelChoice = { model: "m", efforts: ["high"], effort: "high", reasoningVisible: true };
    expect(reasoningIsVisible(off, { opencodeActive: true })).toBe(false);
    expect(reasoningIsVisible(on, { opencodeActive: true })).toBe(true);
  });
});

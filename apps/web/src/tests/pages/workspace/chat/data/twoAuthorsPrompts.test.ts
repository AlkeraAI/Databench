// Two people sent into one chat 59 ms apart. Each prompt row is one person's
// message, and the tape shows each as its own bubble even with no answer
// between them: the fold never joins prompts. (The first row's text already
// holds both messages, because the sender's field was the chat's shared draft
// with the other person's words in it; that is the recorded text, and the
// bubble shows it as recorded.)
import { describe, expect, it } from "vitest";

import {
  cloneTurns,
  createConversationFoldState,
  foldHarnessEvent,
  foldRelayedPrompt,
  setFoldingSeq,
} from "@/pages/workspace/chat/data/harnessEventFold";

const A_TEXT =
  "[explore-sharing] A: write the numbers one to forty as English words, one per line, then the word END-A[explore-sharing] B: write the numbers one to forty as English words, one per line, then the word END-B";
const B_TEXT =
  "[explore-sharing] B: write the numbers one to forty as English words, one per line, then the word END-B";

function fold() {
  const state = createConversationFoldState({ agentHost: "workspace" });
  foldRelayedPrompt(state, { id: "usr:web-14aadcfb-2", text: A_TEXT, at: "2026-09-25T05:17:59.860706Z", seq: 113 });
  foldRelayedPrompt(state, { id: "usr:web-f3536dc9-2", text: B_TEXT, at: "2026-09-25T05:17:59.919489Z", seq: 114 });
  setFoldingSeq(state, 117);
  foldHarnessEvent(state, { event_type: "message.created", message_id: "msg_0d6ff3e600015d0e74VvT26dhy", role: "user" });
  setFoldingSeq(state, 118);
  foldHarnessEvent(state, { event_type: "message.created", message_id: "msg_0d6ffbc0e001GJzOWhp1OYJTgS", role: "user" });
  setFoldingSeq(state, null);
  return cloneTurns(state);
}

describe("two prompts from two people", () => {
  it("are two bubbles, each with its own row's text", () => {
    const bubbles = fold()
      .filter((turn) => turn.author === "user")
      .map((turn) => turn.parts.map((part) => (part.kind === "text" ? part.text : "")).join(""));
    expect(bubbles).toEqual([A_TEXT, B_TEXT]);
  });
});

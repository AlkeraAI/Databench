// One word for the thing a chat waits on.
//
// A reader holding a message nobody had picked up read three names for one
// thing at once: "No machine can serve your organization right now" in the
// banner, "Waiting for the workspace…" under their message, and "Waiting for a
// machine" on the next chat over. Every sentence the chat says about the box
// that runs it — the banner in each state, the line under a waiting message,
// the notes a lost or stopped turn leaves, and the stop that did not land —
// calls it a machine, and none of them calls it a workspace.

import { describe, expect, it } from "vitest";

import { WAITING_FOR_MACHINE } from "@/pages/workspace/chat/ChatSurface";
import { MACHINE_COPY } from "@/pages/workspace/chat/MachineBanner";
import { WORKSPACE_LOST_TURN, WORKSPACE_LOST_TURN_QUIET } from "@/pages/workspace/chat/controller";
import { WORKSPACE_STOPPED_ANSWERING } from "@/pages/workspace/chat/data/harnessEventFold";

function textOf(turn: { parts: readonly { kind: string; text?: string }[] }): string {
  return turn.parts.map((part) => part.text ?? "").join(" ");
}

const SAID: [string, string][] = [
  ["the line under a waiting message", WAITING_FOR_MACHINE],
  ...Object.entries(MACHINE_COPY).map(
    ([state, copy]) => [`the ${state} banner`, `${copy.title} ${copy.body}`] as [string, string],
  ),
  ["the note a lost turn leaves", textOf(WORKSPACE_LOST_TURN)],
  ["the note a turn that timed out leaves", textOf(WORKSPACE_LOST_TURN_QUIET)],
  ["the note a machine that went away leaves", WORKSPACE_STOPPED_ANSWERING],
];

describe("what the chat calls the box it runs on", () => {
  it.each(SAID)("%s says machine, never workspace", (_where, sentence) => {
    expect(sentence).toMatch(/\bmachines?\b/i);
    expect(sentence).not.toMatch(/\bworkspace\b/i);
  });
});

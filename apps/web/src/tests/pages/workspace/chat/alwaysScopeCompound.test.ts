// A compound shell command has no family. Its standing grant records the exact
// text, and the card's always-scope note says so instead of naming the first
// program in it ("every date command"). The ask is raised as the machine
// publishes it and folded by the real fold, so a scope the fold dropped would
// read as the family again here.

import { describe, expect, it } from "vitest";

import type { PermissionConversationPart } from "@alkera/chat-model";
import { findPendingPermissions } from "@alkera/chat-model";

import {
  createConversationFoldState,
  foldHarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { alwaysScope } from "@/pages/workspace/chat/options";

function foldedAsk(subject: Record<string, unknown>): PermissionConversationPart {
  const state = createConversationFoldState();
  foldHarnessEvent(state, { event_type: "turn.started", turn_id: "turn-1" });
  foldHarnessEvent(state, {
    event_type: "permission.request",
    request_id: "perm-1",
    permission_kind: "bash",
    canonical_kind: "shell",
    prompting: true,
    patterns: [String(subject.raw)],
    options: [
      { option_id: "allow_once", name: "Allow once" },
      { option_id: "allow_always", name: "Always allow" },
      { option_id: "reject_once", name: "Reject once" },
    ],
    subject: { capability: "shell", effect: "write", targets: [], reasons: [], ...subject },
  });
  const [ask] = findPendingPermissions(state.turns);
  if (!ask) throw new Error("the fold raised no permission");
  return ask;
}

describe("what always allow covers for a shell command", () => {
  it.each([
    ["a loop piped into a count", "for i in $(seq 1 8); do date >> f; sleep 1; done | wc -l", "date"],
    ["a pipe", "ls | wc -l", "ls"],
    ["an and-list", "git add . && git commit -m wip", "git_add"],
    ["a subshell", "(cd build && make)", "cd"],
  ])("%s covers the exact command", (_label, raw, operation) => {
    const ask = foldedAsk({ raw, operation, scope: "command" });
    expect(alwaysScope("Always allow", ask.subject)).toBe("Always allow covers this exact command.");
  });

  it("a simple command keeps its family", () => {
    const ask = foldedAsk({ raw: "git status", operation: "git_status", scope: "operation" });
    expect(alwaysScope("Always allow", ask.subject)).toBe("Always allow covers every git status command.");
  });

  it("a subject from a writer that predates the scope keeps its family", () => {
    const ask = foldedAsk({ raw: "npm install left-pad", operation: "npm_install" });
    expect(alwaysScope("Always allow", ask.subject)).toBe("Always allow covers every npm install command.");
  });
});

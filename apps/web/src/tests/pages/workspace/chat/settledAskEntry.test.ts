// A permission ask a person answered stays in the transcript as one line: who
// decided, over WHAT was decided on. The question the card asked while it was
// waiting ("Run this command?") must not survive the answer, or a settled ask
// reads as one still waiting on the reader.

import { describe, expect, it, vi } from "vitest";

import type { ConversationTurn, PermissionConversationPart } from "@alkera/chat-model";

import { transcriptEntries, type EntryContext } from "@/pages/workspace/chat/entries";

const ctx: EntryContext = {
  onLinkClick: vi.fn(),
  onResourceOpen: vi.fn(),
  onOpenUrl: vi.fn(),
  onSubagentOpen: vi.fn(),
  onLineageOpen: vi.fn(),
  onKnowledgeOpen: vi.fn(),
  onPlanOpen: vi.fn(),
  questionLive: null,
};

function settled(overrides: Partial<PermissionConversationPart> = {}): PermissionConversationPart {
  return {
    id: "permission-1",
    kind: "permission",
    requestId: "permission-1",
    permissionKind: "bash",
    canonicalKind: "shell",
    patterns: ["ls -la /opt/data"],
    options: [
      { optionId: "allow_once", name: "Allow once" },
      { optionId: "allow_always", name: "Always allow" },
      { optionId: "reject_once", name: "Deny" },
    ],
    status: "resolved",
    asked: true,
    selectedOptionId: "allow_once",
    decidedBy: "user",
    decidedByName: "Sam Lee",
    decidedVia: "web",
    ...overrides,
  };
}

function notice(part: PermissionConversationPart): { title?: unknown; body?: unknown } {
  const turn: ConversationTurn = {
    id: "a1",
    author: "assistant",
    status: "done",
    parts: [part],
  };
  const [entry] = transcriptEntries([turn], ctx);
  expect(entry?.item.kind).toBe("notice");
  return entry?.item as { title?: unknown; body?: unknown };
}

describe("a settled permission ask in the transcript", () => {
  it.each([
    ["a shell command", settled(), "ls -la /opt/data", "Run this command?"],
    [
      "a file edit",
      settled({ permissionKind: "edit", canonicalKind: "edit", patterns: ["src/app.ts"] }),
      "src/app.ts",
      "Edit these files?",
    ],
  ])("names %s it decided on, not the question", (_what, part, subject, question) => {
    const item = notice(part);
    expect(item.body).toBe(subject);
    expect(item.body).not.toBe(question);
    expect(String(item.title)).toMatch(/Sam Lee/);
  });

  it("falls back to what kind of action it was when the ask named nothing", () => {
    const item = notice(settled({ patterns: [] }));
    expect(item.body).toBe("Run a shell command");
  });

  it("leaves no line for an ask nobody was shown", () => {
    const turn: ConversationTurn = {
      id: "a1",
      author: "assistant",
      status: "done",
      parts: [settled({ asked: false })],
    };
    expect(transcriptEntries([turn], ctx)).toEqual([]);
  });
});

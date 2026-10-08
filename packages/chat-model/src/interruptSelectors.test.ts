import { describe, expect, it } from "vitest";
import type {
  ConversationTurn,
  PermissionConversationPart,
  QuestionConversationPart,
} from "./conversation";
import { findActiveQuestion, findPendingPermissions, questionGalleryKey } from "./interruptSelectors";

function turn(parts: ConversationTurn["parts"], id = "t"): ConversationTurn {
  return { id, author: "assistant", parts };
}

function questionPart(overrides: Partial<QuestionConversationPart> = {}): QuestionConversationPart {
  return {
    id: "q",
    kind: "question",
    requestId: "q-1",
    questionKind: "question",
    questions: [{ question: "Q?", header: null, options: [{ label: "A" }, { label: "B" }], multiple: false, custom: false }],
    status: "pending",
    ...overrides,
  };
}

function permissionPart(overrides: Partial<PermissionConversationPart> = {}): PermissionConversationPart {
  return {
    id: "p",
    kind: "permission",
    requestId: "p-1",
    permissionKind: "read",
    canonicalKind: "other",
    patterns: [],
    options: [],
    status: "pending",
    prompting: true,
    ...overrides,
  };
}

describe("findActiveQuestion", () => {
  it("returns the last pending question", () => {
    const older = questionPart({ id: "q-old", requestId: "old" });
    const newer = questionPart({ id: "q-new", requestId: "new" });
    const turns = [turn([older], "t1"), turn([{ id: "x", kind: "text", text: "hi" }, newer], "t2")];
    expect(findActiveQuestion(turns)).toBe(newer);
  });

  it("skips answered and rejected questions", () => {
    const turns = [
      turn([
        questionPart({ id: "q1", status: "answered" }),
        questionPart({ id: "q2", status: "rejected" }),
      ]),
    ];
    expect(findActiveQuestion(turns)).toBeUndefined();
    expect(findActiveQuestion([])).toBeUndefined();
  });
});

describe("findPendingPermissions", () => {
  it("collects only the permissions being prompted", () => {
    const prompting = permissionPart({ id: "p1", requestId: "r1" });
    // Pending but not prompting — still with the policy judge (auto mode); it
    // must NOT raise the interactive queue.
    const judging = permissionPart({ id: "p2", requestId: "r2", prompting: undefined });
    const resolved = permissionPart({ id: "p3", requestId: "r3", status: "resolved" });
    const turns = [turn([prompting, judging, resolved])];
    expect(findPendingPermissions(turns)).toEqual([prompting]);
  });

  it("preserves transcript order across turns", () => {
    const first = permissionPart({ id: "p1", requestId: "r1" });
    const second = permissionPart({ id: "p2", requestId: "r2" });
    const turns = [turn([first], "t1"), turn([second], "t2")];
    expect(findPendingPermissions(turns).map((p) => p.requestId)).toEqual(["r1", "r2"]);
  });
});

describe("questionGalleryKey", () => {
  it("keys on the request id and the question shape", () => {
    expect(questionGalleryKey(questionPart())).toBe("q-1:Q?:A,B");
    // Same request id, one option fewer — the key must move so the gallery
    // remounts instead of answering the old shape.
    expect(
      questionGalleryKey(
        questionPart({
          questions: [{ question: "Q?", header: null, options: [{ label: "A" }], multiple: false, custom: false }],
        }),
      ),
    ).not.toBe("q-1:Q?:A,B");
  });
});

// The webview half of the plan-document seam. A plan approval becomes a host
// document request whose markdown is never empty (the tab says what happened
// rather than opening blank), and the hand-off happens only where a host editor
// exists to receive it.

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { QuestionConversationPart } from "@alkera/chat-model";

const hostMock = vi.hoisted(() => ({
  kind: "browser" as string,
  openPlanDocument: vi.fn(async () => {}),
}));

vi.mock("@/pages/workspace/chat/data", () => ({ chatHost: () => hostMock }));

import { openPlanDocument, planDocumentOf } from "@/pages/workspace/chat/planDocument";

function approval(overrides: Partial<QuestionConversationPart> = {}): QuestionConversationPart {
  return {
    id: "q1",
    kind: "question",
    requestId: "q1",
    questionKind: "plan_approval",
    questions: [
      { question: "Ship the migration in two steps?", header: null, options: [], multiple: false, custom: false },
    ],
    status: "pending",
    planMarkdown: "# The plan\n\n1. Do the thing.",
    ...overrides,
  };
}

beforeEach(() => {
  hostMock.kind = "browser";
  hostMock.openPlanDocument.mockClear();
});

describe("planDocumentOf", () => {
  it("uses the approval's plan markdown, trimmed", () => {
    const part = approval({ planMarkdown: "  # The plan  \n" });
    expect(planDocumentOf(part, "Data audit")).toEqual({
      title: "Data audit plan",
      markdown: "# The plan",
    });
  });

  it.each([
    ["absent", undefined],
    ["blank", "   \n  "],
  ])("falls back to the question text when the markdown is %s", (_case, planMarkdown) => {
    const part = approval({ planMarkdown });
    expect(planDocumentOf(part, "Data audit").markdown).toBe("Ship the migration in two steps?");
  });

  it.each([
    ["no questions at all", []],
    ["a blank question", [{ question: "  ", header: null, options: [], multiple: false, custom: false }]],
  ])("says the approval carried no plan text when there is %s", (_case, questions) => {
    const part = approval({ planMarkdown: undefined, questions });
    const document = planDocumentOf(part, "Data audit");
    expect(document.markdown).toBe("This approval arrived with no plan text."); // pins-source: NO_PLAN_TEXT is module-private and user-visible; a silent rewording must fail here.
    expect(document.markdown).not.toBe("");
  });

  it("titles the document after the chat, one stable title per chat", () => {
    // A stable title per chat is what makes reopening refresh the same tab.
    expect(planDocumentOf(approval(), "Data audit").title).toBe("Data audit plan");
    expect(planDocumentOf(approval({ planMarkdown: "other" }), "Data audit").title).toBe("Data audit plan");
    // An untitled chat falls back to the product name.
    expect(planDocumentOf(approval(), "   ").title).toBe("Databench plan");
  });
});

describe("openPlanDocument", () => {
  it("declines in the browser preview, which has no editor to open into", () => {
    const document = planDocumentOf(approval(), "Data audit");
    expect(openPlanDocument(document)).toBe(false);
    expect(hostMock.openPlanDocument).not.toHaveBeenCalled();
  });

  it("hands the document to the VS Code host and reports it taken", () => {
    hostMock.kind = "vscode";
    const document = planDocumentOf(approval(), "Data audit");
    expect(openPlanDocument(document)).toBe(true);
    expect(hostMock.openPlanDocument).toHaveBeenCalledExactlyOnceWith(document);
  });
});

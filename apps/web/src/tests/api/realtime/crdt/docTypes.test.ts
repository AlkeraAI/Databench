import { describe, expect, it } from "vitest";

import { contentTextOf, LIVE_DOC_TYPES } from "@/api/realtime/crdt/docTypes";

describe("live document types", () => {
  it.each([
    ["chat_draft", "draft"],
    ["file", "content"],
  ] as const)("%s keeps its content in the root text %s", (docType, text) => {
    expect(contentTextOf(docType)).toBe(text);
  });

  it("knows the notebook type", () => {
    expect(Object.keys(LIVE_DOC_TYPES)).toContain("notebook");
  });

  it("refuses to name a single text for a notebook", () => {
    expect(() => contentTextOf("notebook")).toThrow(/no single content text/);
  });
});

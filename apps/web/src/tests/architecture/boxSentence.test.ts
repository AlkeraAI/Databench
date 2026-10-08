// A box's own sentence never reaches the page. The box writes its refusal (and
// the compute plane its machine reason) for whoever runs the box, and it can
// name the platform's hosts and addresses. What a reader is told comes from the
// status the server wrote, which reads the refusal's kind, not its words. So no
// product source reads the sentence fields at all.

import { describe, expect, it } from "vitest";

import { productSources, readSource } from "./scan";

const SCANNED = [
  ...productSources("apps/web/src"),
  ...productSources("packages/ui/src"),
  ...productSources("packages/notebook-ui/src"),
];

const SENTENCE_FIELDS = /\bmachine_refusal_reason\b|\bpublisher_refusal\b/;

describe("the box's own sentence", () => {
  it("is read by no product source", () => {
    const readers = SCANNED.filter((path) => SENTENCE_FIELDS.test(readSource(path)));
    expect(readers).toEqual([]);
  });

  it("is scanned for where the chat's machine is read", () => {
    // The scan is not vacuous: the file that reads the chat's machine is in it.
    expect(SCANNED.some((path) => path.endsWith("pages/workspace/chat/ChatRuntimeLayout.tsx"))).toBe(true);
  });
});

// A 50 MB tool result reaches the browser as a preview: the publisher cut it to
// fit one transcript row. The card has to say so with a SIZE — a reader who is
// told only "truncated" cannot tell a missing line from a missing gigabyte —
// and point at the stored result when there is one to open.

import { describe, expect, it } from "vitest";

import { stepOf, toolPart } from "./_steps";

const FIFTY_MB = 50 * 1024 * 1024;

function blobRef() {
  return [{ handle: "b".repeat(64), name: "output", refType: "text" }];
}

describe("a tool result the publisher had to cut", () => {
  it("says how much of the output is not on screen", () => {
    const step = stepOf(toolPart("bash", { output: "x".repeat(2048), truncatedBytes: FIFTY_MB }));
    expect(step.footer).toContain("50.0 MB");
    expect(step.footer).toContain("not shown here");
  });

  it("points at the stored result when the tool spilled one", () => {
    const step = stepOf(
      toolPart("sql.query", {
        output: { preview: "…" },
        references: blobRef(),
        truncatedBytes: FIFTY_MB,
      }),
    );
    expect(step.footer).toContain("50.0 MB");
    expect(step.footer).toContain("stored result");
  });

  it("does not offer a stored result the tool never produced", () => {
    // opencode's own tools leave the full output on the machine, not in the
    // blob store, so the note must not send a browser reader after a result
    // that is not there.
    const step = stepOf(toolPart("bash", { output: "x".repeat(2048), truncatedBytes: FIFTY_MB }));
    expect(step.footer).toContain("50.0 MB");
    expect(step.footer).not.toContain("stored result");
  });

  it("scales the figure to what was cut", () => {
    const small = stepOf(toolPart("bash", { output: "x", truncatedBytes: 4096 }));
    expect(small.footer).toContain("4 KB");
    expect(small.footer).not.toContain("MB");
  });

  it("claims nothing when the result arrived whole", () => {
    // The negative case the note would be worthless without: an untruncated
    // result must keep whatever footer its own card chose, and never gain one
    // about bytes that were never dropped.
    const step = stepOf(toolPart("bash", { output: "a modest result" }));
    expect(step.footer ?? "").not.toContain("not shown here");
  });

  it("says it for a tool that has no card of its own", () => {
    // The note is stamped at the one door every card resolves through, so a
    // tool nobody designed a card for still tells a reader what is missing.
    const step = stepOf(toolPart("some.future.tool", { output: {}, truncatedBytes: 3 * 1024 * 1024 }));
    expect(step.footer).toContain("3.0 MB");
  });
});

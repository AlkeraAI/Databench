// A line in the transcript states what happened to a turn; it never tells the
// reader to send their question again. The composer is right there, so the
// instruction is filler and appears nowhere.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { REPO_ROOT, productSources } from "./scan";

const RESEND = /\b(send|ask)\b[^"'`\n]{0,20}\b(question|it|message)\s+again\b/i;

/** String literals in `source` that tell the reader to resend. */
export function resendInstructions(source: string): string[] {
  const literals = source.match(/"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`/g) ?? [];
  return literals.filter((literal) => RESEND.test(literal));
}

describe("no transcript line tells the reader to resend", () => {
  it("finds the instruction in a decoy and ignores comments and plain facts", () => {
    expect(
      resendInstructions('const a = "The turn did not finish. Please send the question again.";'),
    ).toHaveLength(1);
    expect(resendInstructions("const b = 'Stopped. Ask it again later.';")).toHaveLength(1);
    expect(
      resendInstructions('// they may send it again\nconst c = "The turn did not finish.";'),
    ).toEqual([]);
  });

  it("holds for every chat source in the web app and the shared ui", () => {
    const found = [
      ...productSources("apps/web/src/pages/workspace/chat"),
      ...productSources("packages/ui/src/chat"),
    ].flatMap((path) =>
      resendInstructions(readFileSync(join(REPO_ROOT, path), "utf-8")).map(
        (literal) => `${path}: ${literal}`,
      ),
    );
    expect(found).toEqual([]);
  });
});

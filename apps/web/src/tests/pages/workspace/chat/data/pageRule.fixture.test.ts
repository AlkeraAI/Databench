// The drift gate between the server's page rule and its mirror here.
//
// `packages/api-core/tests/fixtures/transcript_page/cases.json` is generated
// from the Python rule (`alkera_core.objects.transcript_page.align_boundary`)
// by a seeded generator beside that rule's tests, and a Python test holds the
// fixture to the rule — so a change to the rule regenerates the fixture. This
// replays every case through the TypeScript mirror, so a regenerated fixture
// fails the web until the mirror follows. Nothing else in the repo fails when
// the two sides diverge.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { alignBoundary, type PageRow } from "@/pages/workspace/chat/data/convergence";

const FIXTURE = resolve(
  __dirname,
  "../../../../../../../../packages/api-core/tests/fixtures/transcript_page/cases.json",
);
/** Below this the fixture is not doing its job. */
const AT_LEAST = 200;

interface Case {
  rows: Array<[number, boolean, string | null, boolean]>;
  page_low: number;
  oldest: number;
  reach: { limit: number; turn: number; message: number };
  expect: { seq: number; cut: boolean };
}

interface Fixture {
  rule: string;
  generator: { seed: number; cases: number };
  cases: Case[];
}

function load(): Fixture {
  let text: string;
  try {
    text = readFileSync(FIXTURE, "utf8");
  } catch (error) {
    throw new Error(`the page-rule fixture is missing at ${FIXTURE}: ${String(error)}`);
  }
  const parsed = JSON.parse(text) as Fixture;
  if (!Array.isArray(parsed.cases)) throw new Error("the page-rule fixture holds no cases");
  return parsed;
}

const rowOf = ([seq, startsTurn, messageId, opensMessage]: Case["rows"][number]): PageRow => ({
  seq,
  startsTurn,
  messageId,
  opensMessage,
});

describe("the page rule's generated cases, replayed through the mirror", () => {
  const fixture = load();

  it("is the rule's fixture, and holds enough of it to mean something", () => {
    expect(fixture.rule).toBe("align_boundary");
    expect(fixture.generator.cases).toBe(fixture.cases.length);
    expect(fixture.cases.length).toBeGreaterThanOrEqual(AT_LEAST);
    // Not all one answer: both `cut` values, and floors above the first row.
    expect(fixture.cases.some((c) => c.expect.cut)).toBe(true);
    expect(fixture.cases.some((c) => !c.expect.cut)).toBe(true);
    expect(fixture.cases.some((c) => c.oldest > 1)).toBe(true);
  });

  it("every case answers as the server's rule answered", () => {
    const mismatches: string[] = [];
    fixture.cases.forEach((c, index) => {
      const answer = alignBoundary(c.rows.map(rowOf), {
        pageLow: c.page_low,
        oldest: c.oldest,
        reach: c.reach,
      });
      if (answer.seq !== c.expect.seq || answer.cut !== c.expect.cut) {
        mismatches.push(
          `case ${index}: mirror ${JSON.stringify(answer)} vs rule ${JSON.stringify(c.expect)} ` +
            `(page_low ${c.page_low}, oldest ${c.oldest}, reach ${JSON.stringify(c.reach)}, ${c.rows.length} rows)`,
        );
      }
    });
    expect(mismatches, `${mismatches.length} of ${fixture.cases.length} cases disagree; first:\n${mismatches[0] ?? ""}`).toEqual([]);
  });
});

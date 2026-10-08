// The shared naming corpus, driven through the field's table.
//
// `names.ts` promises that every rule it refuses is "one the Files namespace
// itself refuses (`files.invalid_name.<rule>`), spelled under the SAME rule
// name". Nothing enforced it, and the two had already drifted: a name of three
// spaces was `empty` here and `surrounding_space` on the wire.
//
// One corpus is committed at `packages/shared-openapi/name-rules.cases.json`
// and both sides drive it — this file against `names.ts`, and
// `packages/api-core/tests/files/test_files_names_contract.py` against
// `alkera_core.files.names`. The server is the authority: a disagreement is
// fixed by changing this side.

import { describe, expect, it } from "vitest";

import corpus from "../../shared-openapi/name-rules.cases.json";

import { NAME_MAX_BYTES, SERVER_ENFORCED_NAME_RULES, validateName, windowsSafe } from "./names";

interface Case {
  id: string;
  name: string;
  rule: string | null;
  windowsSafe: boolean;
}

interface Corpus {
  version: number;
  nameMaxBytes: number;
  rules: string[];
  cases: Case[];
}

const CORPUS = corpus as Corpus;

describe("the shared naming corpus", () => {
  it("is worth driving", () => {
    // A corpus that shrank to nothing, or to one verdict, would pass forever.
    expect(CORPUS.cases.length).toBeGreaterThanOrEqual(50);
    expect(new Set(CORPUS.cases.map((c) => c.id)).size).toBe(CORPUS.cases.length);
    const verdicts = new Set(CORPUS.cases.map((c) => c.rule));
    expect([...verdicts].sort()).toEqual([null, ...CORPUS.rules].sort());
    expect(CORPUS.cases.filter((c) => !c.windowsSafe).length).toBeGreaterThanOrEqual(10);
  });

  it("names only rules this table can return", () => {
    expect(CORPUS.rules.slice().sort()).toEqual([...SERVER_ENFORCED_NAME_RULES].sort());
  });

  it("agrees with this side's ceiling", () => {
    // Each side derives `NAME_MAX` less the pull sidecar independently, so a
    // change to the sidecar on one side would otherwise move only one ceiling.
    expect(CORPUS.nameMaxBytes).toBe(NAME_MAX_BYTES);
  });

  it.each(CORPUS.cases.map((c) => [c.id, c] as const))("%s", (_id, testCase) => {
    expect(validateName(testCase.name)?.rule ?? null).toBe(testCase.rule);
    expect(windowsSafe(testCase.name)).toBe(testCase.windowsSafe);
  });
});

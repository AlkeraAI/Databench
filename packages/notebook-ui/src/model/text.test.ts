import { describe, expect, it } from "vitest";

import { sentenceStart } from "./text";

describe("sentenceStart", () => {
  it.each([
    ["filtering needs duckdb", "Filtering needs duckdb"],
    ["  add failed (exit 1)", "Add failed (exit 1)"],
    ["Already capitalised", "Already capitalised"],
    ["polars>=1 not found on PyPI", "Polars>=1 not found on PyPI"],
    ["", ""],
  ])("raises only the first letter of %j", (input, expected) => {
    expect(sentenceStart(input)).toBe(expected);
  });
});

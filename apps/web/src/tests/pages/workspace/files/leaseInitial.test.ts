/**
 * The lease avatar carries the first letter or digit of the holder's name. A
 * chat titled "[walkthrough-2] Run …" showed "[" in front of its badge; leading
 * punctuation and whitespace are skipped, and a name with no letter or digit
 * at all shows the neutral glyph.
 */

import { describe, expect, it } from "vitest";

import { holderInitial, NO_INITIAL } from "@/pages/workspace/files/useLeaseFacet";

describe("holderInitial", () => {
  it.each([
    ["[walkthrough-2] Run", "W"],
    ['"quoted"', "Q"],
    ["  spaced", "S"],
    ["(paren) x", "P"],
    ["émile", "É"],
    ["123 go", "1"],
    ["you", "Y"],
  ])("%j reads %j", (name, expected) => {
    expect(holderInitial(name)).toBe(expected);
  });

  it.each(["[[[", "", "   ", "— ·"])("%j has no letter or digit and shows the neutral glyph", (name) => {
    expect(holderInitial(name)).toBe(NO_INITIAL);
  });
});

import { describe, expect, it } from "vitest";

import { abbreviate } from "./useAbbreviate";

// The shared compact-number formatter a count column leans on. We pin the boundary cases a caller
// actually hits — the roll-over into each unit, the "no suffix under a thousand" floor, fractional
// suffixes, negatives, and the non-finite guard. Casing is Intl's canonical uppercase (K/M/B/T).

describe("abbreviate", () => {
  it.each([
    [0, "0"],
    [1, "1"],
    [999, "999"], // last value with no suffix
    [1_000, "1K"], // the thousands roll-over
    [1_500, "1.5K"],
    [1_234, "1.23K"], // 3 significant digits by default
    [12_345, "12.3K"],
    [999_999, "1M"], // rounds up across the millions boundary
    [1_000_000, "1M"],
    [3_400_000, "3.4M"],
    [1_000_000_000, "1B"],
    [1_500_000_000_000, "1.5T"],
  ])("formats %d as %s", (input, expected) => {
    expect(abbreviate(input)).toBe(expected);
  });

  it("carries the sign of a negative value", () => {
    expect(abbreviate(-4_200)).toBe("-4.2K");
  });

  it("respects a custom significant-digit count", () => {
    expect(abbreviate(1_234, 4)).toBe("1.234K");
    expect(abbreviate(1_234, 2)).toBe("1.2K");
  });

  it("returns an em-dash for a non-finite value rather than 'NaN'", () => {
    expect(abbreviate(Number.NaN)).toBe("—");
    expect(abbreviate(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

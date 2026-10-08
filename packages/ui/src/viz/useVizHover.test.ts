import { describe, expect, it } from "vitest";

import { nearestIndex, sampleFractions } from "./useVizHover";

// The pure geometry behind the viz hover: which sample sits under the pointer. Tested without a DOM
// so the resolution — including the edge clamps and the half-step tie-break — is pinned directly.

describe("sampleFractions", () => {
  it("spreads samples edge-to-edge for a line (first at 0, last at 1)", () => {
    expect(sampleFractions(5)).toEqual([0, 0.25, 0.5, 0.75, 1]);
  });

  it("centres a single sample and yields nothing for an empty series", () => {
    expect(sampleFractions(1)).toEqual([0.5]);
    expect(sampleFractions(0)).toEqual([]);
  });
});

describe("nearestIndex", () => {
  const line = sampleFractions(5); // samples at 0, 25, 50, 75, 100 px in a 100px plot

  it.each([
    [0, 0], // exactly on the first sample
    [10, 0], // within the first half-step
    [13, 1], // just past the midpoint toward sample 1
    [50, 2], // dead centre
    [88, 4], // rounds up toward the last sample
    [100, 4], // exactly on the last sample
  ])("maps x=%dpx to sample %d", (relX, expected) => {
    expect(nearestIndex(relX, 100, line)).toBe(expected);
  });

  it("clamps a pointer past either edge to a valid index", () => {
    expect(nearestIndex(-40, 100, line)).toBe(0);
    expect(nearestIndex(240, 100, line)).toBe(4);
  });

  it("breaks an exact-midpoint tie toward the earlier sample", () => {
    // x=12.5px is equidistant from sample 0 (0px) and sample 1 (25px). The strict-< scan keeps the
    // first-seen index, so the earlier sample wins — a naive <= would flip this to 1.
    expect(nearestIndex(12.5, 100, line)).toBe(0);
  });

  it("resolves against custom (bar-centre) fractions, not an even grid", () => {
    // Two bars centred at 25% and 75%; a pointer at 30px lands on the first, at 60px on the second.
    const bars = [0.25, 0.75];
    expect(nearestIndex(30, 100, bars)).toBe(0);
    expect(nearestIndex(60, 100, bars)).toBe(1);
  });

  it("returns 0 for a degenerate plot (no samples or zero width)", () => {
    expect(nearestIndex(50, 100, [])).toBe(0);
    expect(nearestIndex(50, 0, line)).toBe(0);
  });
});

import { describe, expect, it } from "vitest";

import { doublingDelay } from "./backoff";

describe("doublingDelay", () => {
  it.each([
    { rung: 0, wait: 500 },
    { rung: 1, wait: 1_000 },
    { rung: 2, wait: 2_000 },
    { rung: 3, wait: 3_000 },
    { rung: 30, wait: 3_000 },
  ])("doubles from the floor and holds at the cap: rung $rung waits $wait ms", ({ rung, wait }) => {
    expect(doublingDelay(rung, 500, 3_000)).toBe(wait);
  });

  it("holds at the cap even past the point where the doubling overflows", () => {
    expect(doublingDelay(2_000, 500, 3_000)).toBe(3_000);
  });

  it("doubles without bound when no cap is named", () => {
    expect(doublingDelay(10, 500)).toBe(512_000);
  });

  it("halves below the floor for a rung before the first", () => {
    expect(doublingDelay(-1, 400)).toBe(200);
  });
});

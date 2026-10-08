import { describe, expect, it } from "vitest";

import { formatUsdNanos } from "@alkera/chat-model";

import { perMillionUsd, preciseUsd } from "@/pages/platform/admin/shared/precise";

// The admin console's precise readings keep every digit the wire carries — the
// digits the product's two-decimal rule rounds away on every other page.

describe("perMillionUsd (per-token nanos → USD per 1M tokens)", () => {
  it.each([
    // 15000 nanos/token × 1e6 tokens / 1e9 = $15.00 / 1M
    [15_000, "$15.00"],
    [75_000, "$75.00"],
    [100, "$0.10"],
    // A sub-cent price per 1M keeps every digit, where the product rule would round it.
    [75, "$0.075"],
    [1, "$0.001"],
    [3_125, "$3.125"],
    [0, "$0.00"],
    [1_234_567, "$1,234.567"],
  ])("%d → %s", (perTokenNanos, reads) => {
    expect(perMillionUsd(perTokenNanos)).toBe(reads);
  });
});

describe("preciseUsd (a rate in nanos, every digit)", () => {
  it.each([
    [8_300_000, "$0.0083"],
    [4_000_000, "$0.004"],
    [1, "$0.000000001"],
    [1_500_000_000, "$1.50"],
    [-2_500_000, "-$0.0025"],
  ])("%d → %s", (nanos, reads) => {
    expect(preciseUsd(nanos)).toBe(reads);
  });

  it("keeps what the user-facing rule rounds away", () => {
    expect(formatUsdNanos(8_300_000)).toBe("$0.01");
    expect(preciseUsd(8_300_000)).toBe("$0.0083");
  });

  it.each([null, undefined, 0.5, NaN])("reads %s as the dash", (value) => {
    expect(preciseUsd(value)).toBe("—");
  });
});

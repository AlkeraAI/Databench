import { describe, expect, it } from "vitest";

import {
  creditsByClass,
  extraCreditsLine,
  extraCreditsUsd,
  formatDollars,
  percentOf,
  percentRemaining,
  percentRemainingLabel,
  usageDisplayMode,
} from "@/lib/usageDisplay";

// The one rule every usage surface reads. The table is exhaustive over the
// three inputs so a surface can never be right by accident: the hosted
// self-serve tiers are the ONLY percent posture; Enterprise, a self-hosted
// deployment, and platform staff are money.

describe("usageDisplayMode", () => {
  it.each([
    { tier: "free", selfHosted: false, platformStaff: false, mode: "percent" },
    { tier: "plus", selfHosted: false, platformStaff: false, mode: "percent" },
    { tier: "pro", selfHosted: false, platformStaff: false, mode: "percent" },
    { tier: "enterprise", selfHosted: false, platformStaff: false, mode: "dollars" },
    { tier: "free", selfHosted: true, platformStaff: false, mode: "dollars" },
    { tier: "plus", selfHosted: true, platformStaff: false, mode: "dollars" },
    { tier: "pro", selfHosted: true, platformStaff: false, mode: "dollars" },
    { tier: "enterprise", selfHosted: true, platformStaff: false, mode: "dollars" },
    { tier: "free", selfHosted: false, platformStaff: true, mode: "dollars" },
    { tier: "pro", selfHosted: false, platformStaff: true, mode: "dollars" },
    { tier: "enterprise", selfHosted: false, platformStaff: true, mode: "dollars" },
    { tier: "free", selfHosted: true, platformStaff: true, mode: "dollars" },
  ] as const)(
    "tier=$tier selfHosted=$selfHosted staff=$platformStaff → $mode",
    ({ tier, selfHosted, platformStaff, mode }) => {
      expect(usageDisplayMode({ tier, selfHosted, platformStaff })).toBe(mode);
    },
  );

  it.each([null, undefined, ""])("fails closed to percent while the tier is %s and nothing else says money", (tier) => {
    expect(usageDisplayMode({ tier, selfHosted: false })).toBe("percent");
  });

  it("does not read a tier it does not know as Enterprise", () => {
    expect(usageDisplayMode({ tier: "ENTERPRISE", selfHosted: false })).toBe("percent");
    expect(usageDisplayMode({ tier: "enterprise-trial", selfHosted: false })).toBe("percent");
  });

  it.each([
    { tier: "free", opaqueUsage: true, mode: "percent" },
    { tier: "pro", opaqueUsage: false, mode: "dollars" },
    { tier: "enterprise", opaqueUsage: true, mode: "percent" },
    { tier: "enterprise", opaqueUsage: false, mode: "dollars" },
  ] as const)("on the hosted app the allowance decides: tier=$tier opaque=$opaqueUsage → $mode", ({ tier, opaqueUsage, mode }) => {
    expect(usageDisplayMode({ tier, selfHosted: false, opaqueUsage })).toBe(mode);
  });

  it.each([
    { selfHosted: true, platformStaff: false },
    { selfHosted: false, platformStaff: true },
  ])("an allowance never turns self-hosted or staff views into percent (%o)", (input) => {
    expect(usageDisplayMode({ tier: "free", opaqueUsage: true, ...input })).toBe("dollars");
  });

  it("treats staff as the default false, so a plain member on a hosted Pro org is percent", () => {
    expect(usageDisplayMode({ tier: "pro", selfHosted: false })).toBe("percent");
  });
});

describe("extra credits", () => {
  it("sums every non-plan class at a thousandth of a dollar each", () => {
    expect(extraCreditsUsd({ prepaid_credits: 4200, promotional_credits: 800, on_demand_credits: 25 })).toBeCloseTo(5.025, 6);
  });

  it("counts admin-granted credit as extra credit", () => {
    expect(extraCreditsUsd({ prepaid_credits: 0, admin_cycle_credits: 1_500, admin_permanent_credits: 500 })).toBeCloseTo(2, 6);
  });

  it("breaks the classes out in spend order and leaves empty ones out", () => {
    expect(
      creditsByClass({
        prepaid_credits: 1_000,
        promotional_credits: 0,
        on_demand_credits: 250,
        admin_cycle_credits: 4_000,
        admin_permanent_credits: 2_000,
      }),
    ).toEqual([
      { label: "This cycle's granted credit", usd: 4 },
      { label: "Granted credit", usd: 2 },
      { label: "Purchased credit", usd: 1 },
      { label: "On-demand credit", usd: 0.25 },
    ]);
  });

  it("reads a missing promotional or on-demand class as nothing, not NaN", () => {
    expect(extraCreditsUsd({ prepaid_credits: 12_400 })).toBeCloseTo(12.4, 6);
  });

  it("ignores a negative balance rather than subtracting it from the rest", () => {
    expect(extraCreditsUsd({ prepaid_credits: -500, promotional_credits: 1000 })).toBeCloseTo(1, 6);
  });

  it("has no line at all when the seat holds none — money never appears for its own sake", () => {
    expect(extraCreditsLine({ prepaid_credits: 0, promotional_credits: 0, on_demand_credits: 0 })).toBeNull();
    expect(extraCreditsLine({ prepaid_credits: 12_400 })).toBe("Extra credits · $12.40 left");
  });
});

describe("percentOf", () => {
  it.each([
    { part: 60, whole: 100, pct: 60 },
    { part: 318, whole: 500, pct: 64 },
    { part: 0, whole: 100, pct: 0 },
    { part: 150, whole: 100, pct: 100 },
    { part: -5, whole: 100, pct: 0 },
    // A zero ceiling is a real answer — nothing allotted, nothing left — not a division.
    { part: 0, whole: 0, pct: 100 },
  ])("$part of $whole → $pct%", ({ part, whole, pct }) => {
    expect(percentOf(part, whole)).toBe(pct);
  });

  it("labels the remaining share", () => {
    expect(percentRemainingLabel(40, 100)).toBe("40% remaining");
  });
});

describe("percentRemaining", () => {
  it.each([
    { remaining: 400, limit: 500, pct: 80 },
    { remaining: 0, limit: 500, pct: 0 },
    { remaining: 600, limit: 500, pct: 100 },
    { remaining: -10, limit: 500, pct: 0 },
  ])("$remaining left of $limit → $pct%", ({ remaining, limit, pct }) => {
    expect(percentRemaining(remaining, limit)).toBe(pct);
  });

  // No ceiling means the share is unknown — a pool nothing was granted into has not
  // been spent down, so "0% left" would be a false claim.
  it.each([
    { remaining: 0, limit: 0 },
    { remaining: 5, limit: 0 },
    { remaining: 0, limit: -1 },
  ])("$remaining left of $limit is unknown, not 0%", ({ remaining, limit }) => {
    expect(percentRemaining(remaining, limit)).toBeNull();
    expect(percentRemainingLabel(remaining, limit)).toBeNull();
  });
});

describe("formatDollars", () => {
  it("renders cents precision", () => {
    expect(formatDollars(12.4)).toBe("$12.40");
    expect(formatDollars(0)).toBe("$0.00");
    expect(formatDollars(1234.5)).toBe("$1,234.50");
  });
});

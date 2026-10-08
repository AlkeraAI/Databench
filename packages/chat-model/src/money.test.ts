import { describe, expect, it } from "vitest";

import {
  BELOW_ONE_CENT,
  formatLedgerNanos,
  formatLedgerUsd,
  formatPriceUsd,
  formatUsd,
  formatUsdNanos,
  NANOS_PER_USD,
  nanosToUsdText,
  usdToNanos,
} from "./money";
import vectors from "./moneyVectors.json";

// The vectors are shared with the Python formatter's test, so both languages are
// held to the same readings of the same amounts.

describe("formatUsdNanos", () => {
  it.each(vectors.nanos.map((v) => [v.id, v.nanos, v.reads] as const))("%s", (_id, nanos, reads) => {
    expect(formatUsdNanos(nanos)).toBe(reads);
    expect(formatUsdNanos(BigInt(nanos))).toBe(reads);
    // A number is read exactly too, while it is still an exact integer.
    if (Number.isSafeInteger(Number(nanos))) expect(formatUsdNanos(Number(nanos))).toBe(reads);
  });

  it("reads an amount past a double's exact range from its digits", () => {
    expect(formatUsdNanos(9_007_199_254_740_993_005_000_000n)).toBe("$9,007,199,254,740,993.01");
  });

  it.each([NaN, Infinity, -Infinity])("shows no amount for %s", (value) => {
    expect(formatUsdNanos(value)).toBe("—");
  });
});

describe("formatUsd", () => {
  it.each(vectors.usd.map((v) => [v.id, v.usd, v.reads] as const))("%s", (_id, usd, reads) => {
    expect(formatUsd(usd)).toBe(reads);
  });

  it("rounds the decimal a float prints, where toFixed rounds its binary value", () => {
    // The mechanism this formatter exists for: 1.005 is stored as 1.00499999…, so
    // toFixed(2) reads $1.00 while the amount the product holds is a tie.
    expect((1.005).toFixed(2)).toBe("1.00");
    expect(formatUsd(1.005)).toBe("$1.01");
  });

  it.each(["", " ", ".", "-", "1e", "1.2.3", "$5", "1,000"])("shows no amount for %j", (text) => {
    expect(formatUsd(text)).toBe("—");
  });

  it("names the sub-cent reading once", () => {
    expect(formatUsd(0.001)).toBe(BELOW_ONE_CENT);
  });
});

describe("formatPriceUsd", () => {
  it.each([
    [200, "$200"],
    [0, "$0"],
    ["12.5", "$12.50"],
    [19.999, "$20"],
    [1234.5, "$1,234.50"],
    [0.001, BELOW_ONE_CENT],
    [0.625, "$0.63"],
    [NaN, "—"],
  ] as const)("%s reads %s", (usd, reads) => {
    expect(formatPriceUsd(usd)).toBe(reads);
  });
});

describe("formatLedgerNanos", () => {
  it.each(vectors.ledger_nanos.map((v) => [v.id, v] as const))("%s", (_id, v) => {
    expect(formatLedgerNanos({ granted: v.granted, used: v.used, remaining: v.remaining })).toEqual(v.reads);
  });

  it("adds up on screen whenever the exact figures do", () => {
    // Every granted/used split in whole nanos across a few cents of fractional
    // residue: the displayed remaining is always the displayed granted minus the
    // displayed used.
    const cents = (text: string): number =>
      text === BELOW_ONE_CENT ? 0 : Math.round(Number(text.replace(/[$,]/g, "")) * 100);
    for (let granted = 1_000_000_000; granted <= 1_030_000_000; granted += 1_000_000) {
      for (let used = 0; used <= 30_000_000; used += 1_000_000) {
        const row = formatLedgerNanos({ granted, used, remaining: granted - used });
        expect(cents(row.granted) - cents(row.used)).toBe(cents(row.remaining));
      }
    }
  });

  it("marks a missing figure without dropping the others", () => {
    expect(formatLedgerNanos({ granted: "1000000000", used: "x", remaining: "1000000000" })).toEqual({
      granted: "$1.00",
      used: "—",
      remaining: "$1.00",
    });
  });
});

describe("formatLedgerUsd", () => {
  it("reads the staging pool line from wire decimal strings", () => {
    expect(formatLedgerUsd({ granted: "3000.000000", used: "0.660050", remaining: "2999.339950" })).toEqual({
      granted: "$3,000.00",
      used: "$0.66",
      remaining: "$2,999.34",
    });
  });
});

describe("nanosToUsdText", () => {
  it.each([
    [50_000_000_000, "50"],
    [3_500_000_000, "3.5"],
    [1_234_500_000, "1.2345"],
    [1, "0.000000001"],
    [70, "0.00000007"],
    [0, "0"],
    [-2_500_000_000, "-2.5"],
    ["9007199254740993", "9007199.254740993"],
    [9_007_199_254_740_993n, "9007199.254740993"],
  ] as const)("%s nanos is %s", (nanos, text) => {
    expect(nanosToUsdText(nanos)).toBe(text);
  });

  it.each([1.5, NaN, Infinity, "abc", "1e3", "", "  "])("has no text for %j", (nanos) => {
    expect(nanosToUsdText(nanos)).toBe("");
  });
});

describe("usdToNanos", () => {
  it("is one USD for the factor", () => {
    expect(usdToNanos("1")).toBe(BigInt(NANOS_PER_USD));
    expect(NANOS_PER_USD).toBe(1_000_000_000);
  });

  it.each([
    ["12.34", 12_340_000_000n],
    ["0.000000001", 1n],
    ["9007199.254740993", 9_007_199_254_740_993n],
    ["1000000000", 1_000_000_000_000_000_000n],
    [" 7.5 ", 7_500_000_000n],
    // Past the ninth decimal, a tie rounds away from zero and anything else to the nearest.
    ["0.0000000005", 1n],
    ["0.0000000025", 3n],
    ["0.00000000049", 0n],
    ["-0.0000000025", -3n],
    [0.29, 290_000_000n],
    [1.005, 1_005_000_000n],
  ] as const)("%s USD is %s nanos", (usd, nanos) => {
    expect(usdToNanos(usd)).toBe(nanos);
  });

  it.each(["", "abc", "1.2.3", NaN, Infinity])("is null for %s", (usd) => {
    expect(usdToNanos(usd)).toBeNull();
  });

  it("round-trips through the editor text", () => {
    for (const nanos of [1n, 3_500_000_000n, 9_007_199_254_740_993n]) {
      expect(usdToNanos(nanosToUsdText(nanos))).toBe(nanos);
    }
  });
});

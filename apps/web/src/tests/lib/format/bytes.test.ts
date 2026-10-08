import { describe, expect, it } from "vitest";

import {
  bytesFrom,
  exactBytes,
  formatBytes,
  parseBytes,
  splitBytes,
  UNIT_BYTES,
} from "@/lib/format/bytes";

// The formatter is the one place the product decides how a storage figure reads.
// The bug it exists to close: a ceiling entered as "100 GB" was stored as
// 100 * 2^30 and divided by 10^9 to display, so the admin page said 100 GB and
// the dashboard said 107 GB about the same number. A GB is 10^9 on BOTH sides
// now, so the load-bearing assertions are the exact byte values of the unit
// table and the round trip parse(format(x)) — the ones that would only pass
// under either convention if the two agreed.

describe("the unit table", () => {
  it("is powers of a thousand, because that is how storage is sold", () => {
    expect(UNIT_BYTES).toEqual({
      B: 1,
      KB: 1_000,
      MB: 1_000_000,
      GB: 1_000_000_000,
      TB: 1_000_000_000_000,
      PB: 1_000_000_000_000_000,
    });
    expect(UNIT_BYTES.GB).not.toBe(1024 ** 3);
  });
});

describe("formatBytes", () => {
  it.each([
    [0, "0 B"],
    [1, "1 B"],
    [999, "999 B"],
    [1_000, "1 KB"],
    [1_500, "1.5 KB"],
    [812_000_000, "812 MB"],
    [2_500_000_000, "2.5 GB"],
    [10_000_000_000, "10 GB"],
    [100_000_000_000, "100 GB"],
    [1_000_000_000_000, "1 TB"],
    [5_000_000_000_000, "5 TB"],
    [1_000_000_000_000_000, "1 PB"],
  ])("renders %i as %s", (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected);
  });

  it("reads back the figure the admin page was given, not 7.4% more of it", () => {
    // The two readings that disagreed. The old binary figure is kept alongside
    // so the regression stays legible: it is NOT what "100 GB" means any more.
    expect(formatBytes(100 * 1_000_000_000)).toBe("100 GB");
    expect(formatBytes(100 * 1024 ** 3)).toBe("107.4 GB");
  });

  it.each([
    [999_999_999, "1 GB"],
    [999_500_000_000, "999.5 GB"],
    [999_950_000_000, "1 TB"],
    [1_050_000_000, "1.1 GB"],
    [107_374_182_400, "107.4 GB"],
  ])("rounds %i to one decimal, promoting rather than printing 1000 of a unit: %s", (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected);
  });

  it("says nothing about a figure that is not a size — no ceiling is not zero bytes", () => {
    expect(formatBytes(null)).toBeNull();
    expect(formatBytes(undefined)).toBeNull();
    expect(formatBytes(-1)).toBeNull();
    expect(formatBytes(Number.NaN)).toBeNull();
  });
});

describe("bytesFrom", () => {
  it("scales an entered figure by its unit", () => {
    expect(bytesFrom("2", "TB")).toBe(2_000_000_000_000);
    expect(bytesFrom("10", "GB")).toBe(10_000_000_000);
    expect(bytesFrom("1.5", "GB")).toBe(1_500_000_000);
  });

  it("refuses anything that is not a positive size, so a caller cannot write a ceiling of NaN", () => {
    expect(bytesFrom("", "GB")).toBeNull();
    expect(bytesFrom("   ", "GB")).toBeNull();
    expect(bytesFrom("0", "GB")).toBeNull();
    expect(bytesFrom("-4", "GB")).toBeNull();
    expect(bytesFrom("abc", "GB")).toBeNull();
  });
});

describe("parseBytes", () => {
  it.each([
    ["100", 100_000_000_000],
    ["100 GB", 100_000_000_000],
    ["100GB", 100_000_000_000],
    ["1 TB", 1_000_000_000_000],
    ["0.5 TB", 500_000_000_000],
    ["250 MB", 250_000_000],
    ["  2 tb  ", 2_000_000_000_000],
    ["512 kb", 512_000],
    ["4096 bytes", 4096],
    ["3 t", 3_000_000_000_000],
  ])("reads %s as %i bytes", (text, expected) => {
    expect(parseBytes(text)).toBe(expected);
  });

  it.each([[""], ["   "], ["abc"], ["100 ZB"], ["1,5 GB"], ["-4 GB"], ["0"], ["0 GB"], ["100 GB extra"]])(
    "refuses %s rather than writing something else",
    (text) => {
      expect(parseBytes(text)).toBeNull();
    },
  );

  it("reads a bare number in the unit the caller names", () => {
    expect(parseBytes("250", "MB")).toBe(250_000_000);
  });

  it.each([[10_000_000_000], [100_000_000_000], [1_000_000_000_000], [5_000_000_000_000], [2_500_000_000], [812_000_000]])(
    "round-trips %i through its own rendering",
    (bytes) => {
      const rendered = formatBytes(bytes);
      expect(rendered).not.toBeNull();
      expect(parseBytes(rendered as string)).toBe(bytes);
    },
  );
});

describe("splitBytes", () => {
  it("returns a ceiling in the largest unit that divides it exactly", () => {
    expect(splitBytes(2_000_000_000_000)).toEqual({ amount: "2", unit: "TB" });
    expect(splitBytes(10_000_000_000)).toEqual({ amount: "10", unit: "GB" });
  });

  it("falls back to gigabytes with decimals when neither unit divides it", () => {
    expect(splitBytes(1_500_000_000)).toEqual({ amount: "1.5", unit: "GB" });
  });

  it("round-trips through bytesFrom for a figure an editor seeds and saves untouched", () => {
    const original = 5_000_000_000_000;
    const { amount, unit } = splitBytes(original);
    expect(bytesFrom(amount, unit)).toBe(original);
  });
});

describe("exactBytes", () => {
  it("groups the count an operator is about to commit", () => {
    expect(exactBytes(100_000_000_000)).toBe("100,000,000,000 bytes");
  });
});

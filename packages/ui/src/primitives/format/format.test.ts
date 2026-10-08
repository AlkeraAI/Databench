import { describe, expect, it } from "vitest";

import { formatCount, formatDuration, formatParams, formatTimestamp } from "./format";

// The readings two surfaces share. Each case is a fact a receipt carries, so
// the boundaries matter more than the happy path: the unit a span crosses, the
// digit a group starts at, and what a value the machine never sent turns into.

describe("a count", () => {
  it.each([
    [0, "0"],
    [999, "999"],
    [1000, "1,000"],
    [1234567, "1,234,567"],
    [-4200, "-4,200"],
  ])("groups %s as %s", (value, expected) => {
    expect(formatCount(value)).toBe(expected);
  });

  it("hands back a number it cannot group rather than printing NaN silently", () => {
    expect(formatCount(Number.NaN)).toBe("NaN");
    expect(formatCount(Number.POSITIVE_INFINITY)).toBe("Infinity");
  });
});

describe("a span", () => {
  it.each([
    [0, "0 ms"],
    [412, "412 ms"],
    [999, "999 ms"],
    [1000, "1.0 s"],
    [1234, "1.2 s"],
    [59_900, "59.9 s"],
    [60_000, "1 min 0 s"],
    [95_000, "1 min 35 s"],
    [3_725_000, "62 min 5 s"],
  ])("reads %s ms as %s", (ms, expected) => {
    expect(formatDuration(ms)).toBe(expected);
  });

  it("keeps the sign of a span that came back negative rather than inventing a plausible one", () => {
    expect(formatDuration(-412)).toBe("-412 ms");
    expect(formatDuration(-95_000)).toBe("-1 min 35 s");
  });
});

describe("an instant", () => {
  const AT = "2026-09-07T12:00:00+00:00";

  it("names the zone it is being read in, so the time is answerable", () => {
    // The zone name is what makes the reading a fact rather than a guess about
    // whose clock it is; the exact locale rendering belongs to the reader.
    const reading = formatTimestamp(AT);
    expect(reading).toMatch(/2026/);
    expect(reading).toMatch(/[A-Z]{2,5}[+-]?\d*/);
  });

  it("reads a Date, a millisecond number and an ISO string as the same instant", () => {
    const iso = formatTimestamp(AT);
    expect(formatTimestamp(new Date(AT))).toBe(iso);
    expect(formatTimestamp(new Date(AT).getTime())).toBe(iso);
  });

  it("returns an unreadable stamp as it came — the only copy of it there is", () => {
    expect(formatTimestamp("not a time")).toBe("not a time");
    expect(formatTimestamp("")).toBe("");
  });
});

describe("bound parameters", () => {
  it("reads an object as a list of pairs, in the order it was written", () => {
    expect(formatParams({ customer: "acme", days: 30, live: true })).toEqual([
      { name: "customer", value: "acme" },
      { name: "days", value: "30" },
      { name: "live", value: "true" },
    ]);
  });

  it("groups a numeric bound value the same way every other number is grouped", () => {
    expect(formatParams({ floor: 1234567 })).toEqual([{ name: "floor", value: "1,234,567" }]);
  });

  it("renders a structured value as its JSON rather than [object Object]", () => {
    expect(formatParams({ range: { start: "2026-01-01" } })).toEqual([
      { name: "range", value: '{"start":"2026-01-01"}' },
    ]);
  });

  it("marks a parameter bound to nothing rather than dropping the name", () => {
    expect(formatParams({ customer: null })).toEqual([{ name: "customer", value: "—" }]);
  });

  it.each([
    ["nothing at all", undefined],
    ["a null payload", null],
    ["a list", [1, 2]],
    ["a bare value", "acme"],
  ])("yields no pairs for %s, leaving the absence to the caller", (_case, value) => {
    expect(formatParams(value)).toEqual([]);
  });
});

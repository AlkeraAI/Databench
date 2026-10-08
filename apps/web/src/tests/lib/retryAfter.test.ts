// The one reading of `Retry-After` the event stream and the upload share: both
// wire spellings, "now" for a wait already over, and nothing for a value that
// says nothing.

import { describe, expect, it } from "vitest";

import { parseRetryAfter } from "@/lib/retryAfter";

const NOW = Date.parse("2026-10-05T12:00:00Z");

describe("parseRetryAfter", () => {
  it.each([
    { header: "5", expected: 5_000, why: "a delta in seconds" },
    { header: " 12 ", expected: 12_000, why: "a delta with whitespace around it" },
    { header: "1.5", expected: 1_500, why: "a fractional delta" },
    { header: "0", expected: 0, why: "a delta of nothing" },
    { header: "-3", expected: 0, why: "a negative delta, which means now" },
    { header: "Mon, 05 Oct 2026 12:00:30 GMT", expected: 30_000, why: "an HTTP date ahead" },
    { header: "Mon, 05 Oct 2026 11:59:00 GMT", expected: 0, why: "an HTTP date already past" },
  ])("reads $why ($header) as $expected ms", ({ header, expected }) => {
    expect(parseRetryAfter(header, NOW)).toBe(expected);
  });

  it.each([
    { header: null, why: "no header" },
    { header: "", why: "an empty header" },
    { header: "   ", why: "a blank header" },
    { header: "soon", why: "a word" },
    { header: "5s", why: "a delta with a unit" },
  ])("reads $why as nothing", ({ header }) => {
    expect(parseRetryAfter(header, NOW)).toBeNull();
  });
});

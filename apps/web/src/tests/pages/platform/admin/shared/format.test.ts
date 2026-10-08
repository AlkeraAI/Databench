import { describe, expect, it } from "vitest";

import { formatDateTimeExact } from "@/lib/format/date";
import { usd, count, date, dateTime, dateTimeExact, shortId, windowLabel } from "@/pages/platform/admin/shared/format";

// The registry's readings convert the wire's raw units ONCE so a column reconciles by eye. These pin
// the conversions a wrong one would silently corrupt: nanos → dollars, per-token-nanos → USD/1M, and
// the "—" an absent value reads as (never a broken-looking blank or a raw integer).

describe("usd (nanos → USD)", () => {
  it("converts nanos to a 2-decimal USD string", () => {
    expect(usd(1_500_000_000)).toBe("$1.50");
    expect(usd(412_000_000_000)).toBe("$412.00");
  });
  it("renders zero as $0.00, not a dash", () => {
    expect(usd(0)).toBe("$0.00");
  });
  it("rounds a ledger figure to cents like every user-facing amount", () => {
    expect(usd(2_999_339_950_000)).toBe("$2,999.34");
    expect(usd(1_000)).toBe("<$0.01");
  });
  it("reads null / undefined as the muted dash", () => {
    expect(usd(null)).toBe("—");
    expect(usd(undefined)).toBe("—");
  });
});

describe("count", () => {
  it("groups thousands", () => {
    expect(count(58_204)).toBe("58,204");
  });
});

describe("date / dateTime", () => {
  it("reads an empty value as a dash", () => {
    expect(date(null)).toBe("—");
    expect(dateTime(undefined)).toBe("—");
    expect(date("")).toBe("—");
  });
  it("reads an unparseable value as a dash, not Invalid Date", () => {
    expect(date("not-a-date")).toBe("—");
    expect(dateTime("garbage")).toBe("—");
  });
  it("renders a real ISO timestamp (locale-dependent, but non-empty and not a dash)", () => {
    const out = dateTime("2026-06-30T13:40:00Z");
    expect(out).not.toBe("—");
    expect(out.length).toBeGreaterThan(0);
  });
});

describe("dateTimeExact", () => {
  // Non-zero seconds, so the forensic reading is observably more than the minute one.
  const AT = "2026-06-01T12:00:43Z";

  it("carries the shared policy's seconds-bearing reading", () => {
    expect(dateTimeExact(AT)).toBe(formatDateTimeExact(AT));
    expect(dateTimeExact(AT)).not.toBe(dateTime(AT));
  });

  it("reads an empty value as a dash", () => {
    expect(dateTimeExact(null)).toBe("—");
    expect(dateTimeExact(undefined)).toBe("—");
    expect(dateTimeExact("")).toBe("—");
  });
});

describe("shortId", () => {
  it("takes the leading 8 chars of a uuid", () => {
    expect(shortId("a1b2c3d4-1111-2222-3333-444455556666")).toBe("a1b2c3d4");
  });
});

describe("windowLabel", () => {
  it("maps known windows to a human label and passes an unknown through", () => {
    expect(windowLabel("30d")).toBe("Last 30 days");
    expect(windowLabel("7d")).toBe("Last 7 days");
    expect(windowLabel("custom")).toBe("custom");
  });
});

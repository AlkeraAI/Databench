import { describe, expect, it } from "vitest";

import { readSize, SIZE_TOO_LARGE, MAX_CEILING_BYTES } from "@/lib/format/bytes";
import {
  ABOVE_ZERO,
  BUDGET_TOO_LARGE,
  MAX_BUDGET_USD,
  NOT_PLAIN,
  TWO_DECIMALS,
  readPlainNumber,
  readUsd,
} from "@/lib/format/entry";

import openapi from "../../../../../../packages/shared-openapi/openapi.json";

// A figure is read exactly as typed. The old filter stripped every character that was not a digit
// or a point before anything looked at the text, so "-5" became 5 and "1e3" became 13 — saved
// without a word. These pin that such text is refused with a reason, and that the bounds the
// fields refuse at are the bounds the server's schema names.

describe("readPlainNumber", () => {
  it.each(["12", "12.5", "0.01", "1000000"])("reads %s as itself", (text) => {
    expect(readPlainNumber(text)).toEqual({ value: Number(text), reason: null });
  });

  it("has nothing to say about an empty field", () => {
    expect(readPlainNumber("")).toEqual({ value: null, reason: null });
    expect(readPlainNumber("   ")).toEqual({ value: null, reason: null });
  });

  it.each(["-5", "+5", "1e3", "1E3", "abc", "12.345.6", "1,000", "$5", ".5", "5.", "0x10", "Infinity", "NaN"])(
    "refuses %s rather than reading it as a different figure",
    (text) => {
      expect(readPlainNumber(text)).toEqual({ value: null, reason: NOT_PLAIN });
    },
  );

  it("refuses zero as a figure, since it is a lockout rather than an edit", () => {
    expect(readPlainNumber("0")).toEqual({ value: null, reason: ABOVE_ZERO });
    expect(readPlainNumber("0.00")).toEqual({ value: null, reason: ABOVE_ZERO });
  });
});

describe("readUsd", () => {
  it("reads a budget in whole cents", () => {
    expect(readUsd("12.50")).toEqual({ value: 12.5, reason: null });
    expect(readUsd("7")).toEqual({ value: 7, reason: null });
  });

  it("refuses more than two decimals — a budget is in cents", () => {
    expect(readUsd("1.005")).toEqual({ value: null, reason: TWO_DECIMALS });
  });

  it("refuses a figure past the route's bound, and admits the bound itself", () => {
    expect(readUsd(String(MAX_BUDGET_USD + 1))).toEqual({ value: null, reason: BUDGET_TOO_LARGE });
    expect(readUsd(String(MAX_BUDGET_USD))).toEqual({ value: MAX_BUDGET_USD, reason: null });
  });

  it("keeps the plain-number refusals ahead of the budget ones", () => {
    expect(readUsd("-5")).toEqual({ value: null, reason: NOT_PLAIN });
    expect(readUsd("1e3")).toEqual({ value: null, reason: NOT_PLAIN });
    expect(readUsd("0")).toEqual({ value: null, reason: ABOVE_ZERO });
  });
});

describe("readSize", () => {
  it("scales a plain figure by its unit", () => {
    expect(readSize("2", "TB")).toEqual({ value: 2_000_000_000_000, reason: null });
    expect(readSize("1.5", "GB")).toEqual({ value: 1_500_000_000, reason: null });
  });

  it.each(["-4", "1e3", "abc", "1,5"])("refuses %s as typed", (text) => {
    expect(readSize(text, "GB")).toEqual({ value: null, reason: NOT_PLAIN });
  });

  it("refuses zero and a figure past the route's bound, and admits the bound itself", () => {
    expect(readSize("0", "GB")).toEqual({ value: null, reason: ABOVE_ZERO });
    expect(readSize("1001", "PB" as never)).toEqual({ value: null, reason: SIZE_TOO_LARGE });
    expect(readSize("1000000001", "GB")).toEqual({ value: null, reason: SIZE_TOO_LARGE });
    expect(readSize("1000000000", "GB")).toEqual({ value: MAX_CEILING_BYTES, reason: null });
  });
});

describe("the bounds the fields refuse at are the server's", () => {
  // The OpenAPI document is the server's own statement of what a figure field takes: each bounded
  // figure carries `maximum`. A drift in either direction fails here.
  type Prop = { anyOf?: { maximum?: number }[]; maximum?: number };
  const schemas = (openapi as unknown as { components: { schemas: Record<string, { properties: Record<string, Prop> }> } }).components.schemas;
  const maximumOf = (schema: string, field: string): number | undefined => {
    const prop = schemas[schema].properties[field];
    return prop.maximum ?? prop.anyOf?.find((p) => p.maximum != null)?.maximum;
  };

  it.each(["TeamAllocationUpdate", "MemberBudgetRequest", "TeamMemberLimitRequest"])("names the same budget bound %s names", (schema) => {
    // A budget is written as decimal USD; the schema names its bound in whole USD.
    type UsdProp = { "x-maximum-usd"?: number; anyOf?: { "x-maximum-usd"?: number }[] };
    const prop = schemas[schema].properties.amount_usd as unknown as UsdProp;
    const bound = prop["x-maximum-usd"] ?? prop.anyOf?.find((p) => p["x-maximum-usd"] != null)?.["x-maximum-usd"];
    expect(bound).toBe(MAX_BUDGET_USD);
    // The same figure the server names in its own refusal ("at most $1,000,000,000").
    expect(BUDGET_TOO_LARGE).toBe("At most $1,000,000,000 per cycle.");
  });

  it.each(["TeamAllocationUpdate", "StorageLimitUpdate", "OrgStorageLimitUpdate"])(
    "names the same storage bound %s names",
    (schema) => {
      expect(maximumOf(schema, "limit_bytes")).toBe(MAX_CEILING_BYTES);
    },
  );
});

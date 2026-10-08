// The expression check, held to the shared corpus the Python profile walks:
// every case the profile admits is admitted here with the same fields, every
// case it refuses is refused here, and the allowlist is the profile's own.

import { describe, expect, it } from "vitest";

import { readExpressions as read } from "./corpus";
import {
  checkExpression,
  EXPRESSION_CONSTANTS,
  EXPRESSION_FUNCTIONS,
  MAX_EXPRESSION_ARGS,
  MAX_EXPRESSION_CHARS,
  MAX_EXPRESSION_DEPTH,
  MAX_PAD_LENGTH,
} from "./expression";
import { guardSpec } from "./guard";

interface Admit {
  name: string;
  expression: string;
  fields: string[];
}
interface Refuse {
  name: string;
  expression: string;
}

const admit = read<Admit[]>("admit");
const refuse = read<Refuse[]>("refuse");

/** The three places a spec carries an expression, and where each is refused. */
const SLOTS: ReadonlyArray<[string, (e: string) => Record<string, unknown>, string]> = [
  ["calculate", (e) => ({ mark: "point", transform: [{ calculate: e, as: "out" }] }), "transform[0].calculate"],
  ["filter", (e) => ({ mark: "point", transform: [{ filter: e }] }), "transform[0].filter"],
  [
    "test",
    (e) => ({ mark: "point", encoding: { color: { condition: { test: e, value: "red" }, value: "blue" } } }),
    "encoding.color.condition.test",
  ],
];

describe("checkExpression over the shared corpus", () => {
  it("has the corpus", () => {
    expect(admit.length).toBeGreaterThanOrEqual(30);
    expect(refuse.length).toBeGreaterThanOrEqual(60);
  });

  it.each(admit.map((c) => [c.name, c] as const))("admits %s with the fields it reads", (_name, c) => {
    expect(checkExpression(c.expression)).toEqual({ ok: true, fields: c.fields });
  });

  it.each(refuse.map((c) => [c.name, c] as const))("refuses %s without repeating it", (_name, c) => {
    const verdict = checkExpression(c.expression);
    expect(verdict.ok).toBe(false);
    const text = c.expression.trim();
    if (!verdict.ok && text.length > 3) expect(verdict.reason).not.toContain(text);
  });

  it("uses the profile's own allowlist and limits", () => {
    expect(read("allowlist")).toEqual({
      functions: [...EXPRESSION_FUNCTIONS].sort(),
      constants: [...EXPRESSION_CONSTANTS].sort(),
      max_chars: MAX_EXPRESSION_CHARS,
      max_depth: MAX_EXPRESSION_DEPTH,
      max_args: MAX_EXPRESSION_ARGS,
      max_pad_length: MAX_PAD_LENGTH,
    });
  });

  it("refuses a value that is not a string", () => {
    expect(checkExpression(5)).toMatchObject({ ok: false });
  });
});

describe("guardSpec reads expressions in every slot", () => {
  for (const [slot, build, path] of SLOTS) {
    it.each(admit.map((c) => [c.name, c] as const))(`admits %s as a ${slot}`, (_name, c) => {
      expect(guardSpec(build(c.expression))).toEqual({ ok: true });
    });
    it.each(refuse.map((c) => [c.name, c] as const))(`refuses %s as a ${slot}`, (_name, c) => {
      expect(guardSpec(build(c.expression))).toMatchObject({ ok: false, path });
    });
  }
});

// The sidebar's logo starts on the same column as everything below it: the section labels, the nav
// icons and the account avatar. jsdom does not lay out, so the contract is pinned on the rules that
// produce it — the head's left padding must equal the nav's inset plus a nav item's own padding,
// resolved through the spacing tokens, so changing either side without the other fails here.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const PORTAL = readFileSync(join(HERE, "../../styles/portal.css"), "utf8");
const TOKENS = readFileSync(join(HERE, "../../../../../packages/ui/src/theme/tokens.css"), "utf8");

function rule(selector: string): string {
  const at = PORTAL.indexOf(`${selector} {`);
  expect(at, selector).toBeGreaterThan(-1);
  return PORTAL.slice(at, PORTAL.indexOf("}", at));
}

function token(name: string): number {
  const match = TOKENS.match(new RegExp(`--${name}:\\s*(\\d+)px`));
  expect(match, name).not.toBeNull();
  return Number(match![1]);
}

/** Pixels of a length written as `Npx`, `var(--t)` or `calc(var(--a) + var(--b))`. */
function px(value: string): number {
  return [...value.matchAll(/var\(--(\w+)\)|(\d+)px/g)].reduce(
    (sum, m) => sum + (m[1] ? token(m[1]) : Number(m[2])),
    0,
  );
}

/** The left component of a `padding` shorthand. */
function paddingLeft(body: string): number {
  const value = body.match(/padding:\s*([^;]+);/)![1].trim();
  // Split on top-level spaces only: `calc(var(--a) + var(--b))` is one component.
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  for (const ch of value) {
    if (ch === "(") depth++;
    if (ch === ")") depth--;
    if (ch === " " && depth === 0) {
      if (current) parts.push(current);
      current = "";
    } else current += ch;
  }
  if (current) parts.push(current);
  const left = parts.length === 4 ? parts[3] : parts.length >= 2 ? parts[1] : parts[0];
  return px(left!);
}

describe("the sidebar brand", () => {
  it("starts on the column the nav labels, icons and avatar start on", () => {
    const column = paddingLeft(rule(".alk-nav")) + paddingLeft(rule(".alk-navitem"));
    expect(paddingLeft(rule(".alk-sidebar__head"))).toBe(column);
    expect(column).toBe(24);
  });
});

// jsdom does not lay out, so the no-horizontal-scroll contract is pinned on the rules that produce
// it; the live proof is the Playwright rig (see fix_a11y.md). `/analytics/usage` put its window
// switch (`7 days … All time`) in the masthead actions slot; the slot was `flex: 0 0 auto`, so it
// sized to its controls' full width and pushed the page past the viewport at every width below the
// desktop layout — 460px over at 390, 110px at 1024. The allowance therefore has to sit in the BASE
// rule, unconditioned: a copy inside `@media (max-width: 900px)` leaves 901–1439px broken, which is
// exactly how the first attempt at this fix passed its own test and still scrolled at 1024.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const PORTAL = readFileSync(join(HERE, "../../styles/portal.css"), "utf8");

/** The stylesheet with every `@media` block cut out — what applies at EVERY viewport width. */
const UNCONDITIONED = (() => {
  let out = "";
  let i = 0;
  while (i < PORTAL.length) {
    const at = PORTAL.indexOf("@media", i);
    if (at === -1) {
      out += PORTAL.slice(i);
      break;
    }
    out += PORTAL.slice(i, at);
    // Walk the query's braces to its close so nested rules go with it.
    let depth = 0;
    let j = PORTAL.indexOf("{", at);
    for (; j < PORTAL.length; j++) {
      if (PORTAL[j] === "{") depth++;
      else if (PORTAL[j] === "}" && --depth === 0) break;
    }
    i = j + 1;
  }
  return out;
})();

/** The body of the `.alk-top__actions { … }` rule in a given sheet. */
function actionsBlock(sheet: string): string {
  const at = sheet.indexOf(".alk-top__actions {");
  expect(at).toBeGreaterThan(-1);
  return sheet.slice(at, sheet.indexOf("}", at));
}

describe("the masthead actions slot", () => {
  it("may shrink below its controls at every width, not only the narrow ones", () => {
    const block = actionsBlock(UNCONDITIONED);
    expect(block).toMatch(/min-width:\s*0/);
    // A zero shrink factor is exactly what made the slot immovable.
    expect(block).not.toMatch(/flex:\s*0\s+0\s/);
    expect(block).toMatch(/flex:\s*0\s+1\s/);
  });

  it("wraps its controls onto further rows once the row is narrow", () => {
    const narrow = PORTAL.slice(PORTAL.indexOf("@media (max-width: 900px)"));
    expect(actionsBlock(narrow)).toMatch(/flex-wrap:\s*wrap/);
  });
});

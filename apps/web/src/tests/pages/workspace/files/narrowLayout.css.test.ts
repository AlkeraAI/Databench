// Files at 390: the search-scope dropdown ran 24px past the right edge with its chevron cut off,
// and the one-line places strip stood 280px tall over an empty band that pushed the listing a third
// of the way down the screen. jsdom does not lay out, so the contract is pinned on the rules.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(
  join(HERE, "../../../../pages/workspace/files/files-page.css"),
  "utf8",
);

/** Every `@media (max-width: 900px)` block in the sheet, joined — the narrow layout's rules. */
const NARROW = (() => {
  const blocks: string[] = [];
  let from = 0;
  for (;;) {
    const at = CSS.indexOf("@media (max-width: 900px)", from);
    if (at === -1) break;
    let depth = 0;
    let end = CSS.indexOf("{", at);
    for (; end < CSS.length; end++) {
      if (CSS[end] === "{") depth++;
      else if (CSS[end] === "}" && --depth === 0) break;
    }
    blocks.push(CSS.slice(at, end));
    from = end;
  }
  return blocks.join("\n");
})();

function rule(sheet: string, selector: string): string {
  const at = sheet.indexOf(`${selector} {`);
  expect(at, `no narrow ${selector} rule`).toBeGreaterThan(-1);
  return sheet.slice(at, sheet.indexOf("}", at));
}

describe("the Files page below the portal breakpoint", () => {
  it("lets the search field give up width so the scope dropdown stays on screen", () => {
    const field = rule(NARROW, ".alk-files-search-input");
    expect(field).toMatch(/min-width:\s*0/);
    // The dropdown may shrink from its 12rem, never grow past it.
    expect(rule(NARROW, ".alk-files-search__scope")).toMatch(/flex:\s*0\s+1\s+12rem/);
  });

  it("gives the places strip its own height and the listing the rest", () => {
    expect(rule(NARROW, '.alk-files[data-layout="narrow"] .alk-files__body')).toMatch(
      /grid-template-rows:\s*auto\s+minmax\(0,\s*1fr\)/,
    );
  });
});

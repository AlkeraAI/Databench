// At 390 the organization-settings strip wrapped "Single sign-on" onto two lines and "Audit log"
// onto two more. A tab strip wider than its box scrolls sideways with every label on one line.
// jsdom does not lay out, so the contract is pinned on the rules that produce it.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(join(HERE, "tabs.css"), "utf8");

function rule(selector: string): string {
  const at = CSS.indexOf(`${selector} {`);
  expect(at, `no ${selector} rule`).toBeGreaterThan(-1);
  return CSS.slice(at, CSS.indexOf("}", at));
}

describe("the tab strip's overflow", () => {
  it("scrolls sideways rather than growing past its box", () => {
    expect(rule(".alk-tabs")).toMatch(/overflow-x:\s*auto/);
  });

  it("never wraps or squeezes a tab's label", () => {
    const tab = rule('.alk-tabs > [role="tab"]');
    expect(tab).toMatch(/white-space:\s*nowrap/);
    expect(tab).toMatch(/flex:\s*0\s+0\s+auto/);
  });

  it("keeps the active marker inside the scroller, where it is not clipped", () => {
    // A scroller clips its overflow; a marker hung a pixel below the box would be cut in half.
    expect(rule(".alk-tabs__marker")).toMatch(/bottom:\s*0/);
  });
});

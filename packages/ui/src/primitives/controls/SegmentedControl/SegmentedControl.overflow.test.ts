// The control documents that "a label that outgrows its 1fr track truncates with an ellipsis rather
// than widening the control" — but nothing bounded the track itself, so a four-option switch in a
// narrow masthead sized to its labels and carried the page 460px past a 390px viewport. jsdom does
// not lay out, so the bound is pinned on the rule.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(join(HERE, "segmentedcontrol.css"), "utf8");

/** The body of the base `.alk-seg { … }` rule. */
function baseBlock(): string {
  const at = CSS.indexOf(".alk-seg {");
  expect(at).toBeGreaterThan(-1);
  return CSS.slice(at, CSS.indexOf("}", at));
}

describe("the segmented control's width", () => {
  it("never exceeds the box it is placed in", () => {
    expect(baseBlock()).toMatch(/max-width:\s*100%/);
  });

  it("lets its tracks shrink so the labels take the ellipsis the control documents", () => {
    // `min-width: 0` on the button is what allows a 1fr track to drop below its content; without it
    // the max-width above would clip instead of truncating.
    const at = CSS.indexOf(".alk-seg button {");
    expect(CSS.slice(at, CSS.indexOf("}", at))).toMatch(/min-width:\s*0/);
  });
});

/** The body of the `@media (max-width: 900px) { … }` block, braces walked so nested rules come too. */
function narrowBlock(): string {
  const at = CSS.indexOf("@media (max-width: 900px)");
  expect(at).toBeGreaterThan(-1);
  let depth = 0;
  let end = CSS.indexOf("{", at);
  for (; end < CSS.length; end++) {
    if (CSS[end] === "{") depth++;
    else if (CSS[end] === "}" && --depth === 0) break;
  }
  return CSS.slice(at, end);
}

/** One rule's body inside a sheet fragment. */
function ruleIn(sheet: string, selector: string): string {
  const at = sheet.indexOf(`${selector} {`);
  expect(at, `no ${selector} rule`).toBeGreaterThan(-1);
  return sheet.slice(at, sheet.indexOf("}", at));
}

describe("the segmented control below the portal breakpoint", () => {
  // Six tabs at 390 read "Ove… Cre… Me… Acti… Usa… Sett…", with the names only in a hover title a
  // touch reader never sees. Narrow, every label stays whole and the strip scrolls in its own box.
  it("sizes each track to its whole label and scrolls sideways instead of truncating", () => {
    const narrow = narrowBlock();
    const root = ruleIn(narrow, ".alk-seg");
    expect(root).toMatch(/grid-auto-columns:\s*minmax\(max-content,\s*1fr\)/);
    expect(root).toMatch(/overflow-x:\s*auto/);
    const label = ruleIn(narrow, ".alk-seg__label");
    expect(label).toMatch(/overflow:\s*visible/);
    expect(label).not.toMatch(/text-overflow:\s*ellipsis/);
  });

  it("marks the active option on the cell itself, since unequal tracks break the sliding marker", () => {
    const narrow = narrowBlock();
    expect(ruleIn(narrow, ".alk-seg__ind")).toMatch(/display:\s*none/);
    expect(ruleIn(narrow, ".alk-seg button[data-on]")).toMatch(/background:/);
  });
});

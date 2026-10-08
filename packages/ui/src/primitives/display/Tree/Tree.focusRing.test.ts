// The tree moves the focus ring off the focused treeitem onto its visible row line, so the ring
// wraps the row rather than the whole subtree. The treeitem turns its own outline off, and the row
// is never `:focus-visible` itself, so the theme's blanket `:where(:focus-visible)` rule cannot
// reach it: the row must draw the ring explicitly or every tree loses its keyboard indicator.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(join(HERE, "tree.css"), "utf8");

describe("the tree row's focus ring", () => {
  it("paints the shared focus ring on the row the focused item owns", () => {
    const at = CSS.indexOf(".alk-tree__node:focus-visible > .alk-tree__row {");
    expect(at).toBeGreaterThan(-1);
    const block = CSS.slice(at, CSS.indexOf("}", at));
    expect(block).toMatch(/outline:\s*2px solid var\(--alkFocusRing\)/);
    expect(block).toMatch(/outline-offset:\s*-2px/);
  });
});

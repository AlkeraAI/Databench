// The permission card's action bar wraps instead of clipping.
//
// With the mode chip on it the bar is wider than a narrow conversation column;
// held to one line, the allow key is cut at the column's edge and the caret
// that opens "Always allow" lands off screen. jsdom lays
// nothing out, so the check reads the sheet and pins the declarations the
// wrap depends on: the row may wrap, the spacer claims no width (a growing
// spacer with a width of its own takes a line by itself), and the allow group
// keeps the trailing edge on whichever line it lands.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "permission.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

/** Every top-level declaration block for one exact selector, joined. */
function rules(selector: string): string {
  const blocks: string[] = [];
  let from = 0;
  for (;;) {
    const at = css.indexOf(`\n${selector} {`, from);
    if (at < 0) break;
    const open = css.indexOf("{", at);
    const close = css.indexOf("}", open);
    blocks.push(css.slice(open + 1, close));
    from = close;
  }
  expect(blocks.length, `no rule for \`${selector}\``).toBeGreaterThan(0);
  return blocks.join("\n");
}

describe("the permission card's action bar", () => {
  it("wraps onto a second line rather than clipping its keys", () => {
    expect(rules(".chat-root .chat-permission-bar")).toMatch(/flex-wrap:\s*wrap/);
  });

  it("has a spacer that claims no width, so it never takes a line of its own", () => {
    const gap = rules(".chat-root .chat-permission-bar__gap");
    expect(gap).toMatch(/flex:\s*1 1 0/);
    expect(gap).toMatch(/min-width:\s*0/);
  });

  it("keeps the allow group at the trailing edge on whichever line it lands", () => {
    expect(rules(".chat-root .chat-permission-allow")).toMatch(/margin-left:\s*auto/);
  });
});

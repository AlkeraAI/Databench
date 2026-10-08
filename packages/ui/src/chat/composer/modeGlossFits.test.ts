// Every stance's gloss reads whole in the mode menu.
//
// The gloss is clamped so a described row's height stays closed-form, and at
// two lines the longest one, Auto's ("Works on its own and pauses only for
// risky or destructive steps."), was cut off mid-sentence. The menu sizes to
// its content up to its cap, and a gloss is always long enough to reach it.
// jsdom lays nothing out, so this measures the way the sheet does: the gloss
// column the menu leaves at that width, a generous average
// glyph width for the reading face, and the lines each shipped gloss needs,
// which must fit under the clamp.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { PERMISSION_MODES } from "@alkera/chat-model";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "composer.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

function rule(selector: string): string {
  const at = css.indexOf(`\n${selector} {`);
  expect(at, `no rule for ${selector}`).toBeGreaterThan(-1);
  const open = css.indexOf("{", at);
  return css.slice(open + 1, css.indexOf("}", open));
}

const px = (block: string, property: string): number => {
  const match = new RegExp(`(?:^|;|\\n)\\s*${property}:\\s*([^;]+);`).exec(block);
  expect(match, `no ${property}`).not.toBeNull();
  const value = match![1]!.trim();
  const min = /min\((\d+)px/.exec(value);
  if (min) return Number(min[1]);
  const plain = /^(\d+)px$/.exec(value);
  if (plain) return Number(plain[1]);
  throw new Error(`cannot read ${property}: ${value}`);
};

/** The reading face's average glyph at 14px is under 7px; 7.5 leaves room. */
const GLYPH_PX = 7.5;
/** The menu's and the row's padding, both sides, at the 8px step the sheet uses. */
const PADDING_PX = 2 * 8 + 2 * 8;
/** The icon column and the gap after it (`grid-template-columns: 20px …`). */
const ICON_PX = 20 + 8;

describe("the mode menu's glosses", () => {
  it("each fits under the clamp at the width the menu opens at", () => {
    const width = px(rule(".chat-root .chat-composer-pop"), "max-width");
    const clamp = Number(/line-clamp:\s*(\d+)/.exec(rule(".chat-root .chat-composer-opt__desc"))![1]);
    const perLine = Math.floor((width - PADDING_PX - ICON_PX) / GLYPH_PX);
    for (const mode of PERMISSION_MODES) {
      const words = (mode.description ?? "").split(" ");
      let lines = 1;
      let used = 0;
      for (const word of words) {
        const next = used === 0 ? word.length : used + 1 + word.length;
        if (next > perLine) {
          lines += 1;
          used = word.length;
        } else used = next;
      }
      expect(lines, `${mode.label}: "${mode.description}"`).toBeLessThanOrEqual(clamp);
    }
  });
});

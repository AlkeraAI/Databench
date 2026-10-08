// A notebook ask with many cells stays inside the permission card.
//
// The card caps its height and its subject is what gives: the command and
// details insets shrink and scroll, so the action bar always shows. The
// notebook ask did not take part, and twenty cells ran past the card's bottom
// edge with Allow and Deny under them. jsdom lays nothing out, so the check
// reads the sheet and pins what the contract needs: the ask may shrink below
// its content, and its cell list scrolls.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "notebookAsk.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

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

describe("a notebook ask in the permission card", () => {
  it("shrinks with the card instead of pushing past it", () => {
    const ask = rules(".chat-root .chat-notebook-ask");
    expect(ask).toMatch(/flex:\s*1 1 auto/);
    expect(ask).toMatch(/min-height:\s*0/);
  });

  it("scrolls its cell list once the card has no more room", () => {
    const list = rules(".chat-root .chat-notebook-ask > .chat-notebook-ask__cells");
    expect(list).toMatch(/min-height:\s*0/);
    expect(list).toMatch(/overflow-y:\s*auto/);
    expect(list).not.toMatch(/flex:\s*none/);
  });
});

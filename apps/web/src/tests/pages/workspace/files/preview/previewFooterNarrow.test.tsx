/**
 * The preview's action row keeps every action on screen at a phone's width.
 *
 * The shared modal's footer is a right-aligned flex row that neither wraps nor
 * scrolls. The file preview puts up to seven actions in it, so at 390 px the
 * ones that did not fit overflowed off the LEFT edge — Download and Open in
 * Files sat at negative x, where no scroll can reach them, and a phone user
 * could not download a file from its preview. The preview's row wraps instead.
 *
 * jsdom does not lay out, so the declaration the layout rests on is read off
 * the sheet that states it, and the modal is rendered to prove that selector
 * actually lands on the footer the actions are drawn in.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { Button, Modal } from "@alkera/ui";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

const SELECTOR = ".files-preview .alk-modal__foot";

const PAGE_CSS = readFileSync(
  join(process.cwd(), "src/pages/workspace/files/files-page.css"),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

/** The body of the top-level rule whose prelude is exactly `selector`. Nested
 *  rules (a media query) are not top level, so a wrap that only applied at
 *  some widths would not satisfy this. */
function topLevelRule(css: string, selector: string): string | undefined {
  let depth = 0;
  let head = 0;
  let start = 0;
  let prelude = "";
  for (let i = 0; i < css.length; i += 1) {
    if (css[i] === "{") {
      if (depth === 0) {
        prelude = css.slice(head, i).trim();
        start = i + 1;
      }
      depth += 1;
    } else if (css[i] === "}") {
      depth -= 1;
      if (depth === 0) {
        if (prelude === selector) return css.slice(start, i);
        head = i + 1;
      }
    }
  }
  return undefined;
}

describe("the preview's action row at a narrow width", () => {
  it("wraps at every width rather than overflowing off the left edge", () => {
    const rule = topLevelRule(PAGE_CSS, SELECTOR);
    expect(rule, `${SELECTOR} has no top-level rule`).toBeDefined();
    expect(rule).toMatch(/flex-wrap:\s*wrap/);
  });

  it("names the element the preview's actions are drawn in", () => {
    // The same props the preview passes the modal: its class and a footer.
    render(
      <Modal open onClose={() => undefined} title="notes.md" size="full" className="files-preview" footer={<Button>Download</Button>}>
        <p>body</p>
      </Modal>,
    );
    const foot = document.querySelector(SELECTOR);
    expect(foot).not.toBeNull();
    expect(foot).toContainElement(screen.getByRole("button", { name: "Download" }));
  });
});

import { cleanup, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { Modal } from "./Modal";

// The dialog's sizes, and the one that is not a dialog-shaped card at all.
//
// `full` exists for LOOKING at something — a file preview — rather than for filling
// something in. A preview inside a 488px card is a thumbnail, so this size takes the
// viewport it is given up to a reading ceiling, and below the width where a card
// still makes sense it becomes the screen. None of that is expressible in the
// component: the width and the sheet are the stylesheet's, so the stylesheet is
// what the test reads.

afterEach(cleanup);

const CSS = readFileSync(join(__dirname, "modal.css"), "utf8");

/** The body of the rule whose selector ENDS with `selector`, inside `scope` (the whole
 *  sheet by default). The selector must be followed by the brace, so a rule that merely
 *  mentions it inside a `:has()` is not mistaken for it. `null` when no such rule is there. */
function ruleFor(selector: string, scope: string = CSS): string | null {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`${escaped}\\s*\\{([^}]*)\\}`).exec(scope);
  return match ? match[1]! : null;
}

/** The text of the `max-width: 900px` media block — where a card stops being a card. */
function narrowBlock(): string {
  const at = CSS.indexOf("@media (max-width: 900px)");
  if (at < 0) return "";
  // Walk the braces so the whole block comes back, not just its first rule.
  let depth = 0;
  for (let i = CSS.indexOf("{", at); i < CSS.length; i += 1) {
    if (CSS[i] === "{") depth += 1;
    if (CSS[i] === "}") {
      depth -= 1;
      if (depth === 0) return CSS.slice(at, i + 1);
    }
  }
  return "";
}

describe("Modal size", () => {
  it("stamps the size it was given on the surface", () => {
    render(
      <Modal open onClose={() => {}} title="Chart" size="full">
        <p>Body.</p>
      </Modal>,
    );
    expect(screen.getByRole("dialog").dataset.size).toBe("full");
  });

  it("gives the full size the viewport, up to a reading ceiling", () => {
    const rule = ruleFor('.alk-modal[data-size="full"]');
    expect(rule).not.toBeNull();
    // Width and height are both bounded: an unbounded width on a 4K screen puts a
    // 3,800px line of text on the page, and an unbounded height pushes the action
    // row off the bottom.
    expect(rule).toMatch(/--alk-modal-w:\s*min\(96vw,\s*1600px\)/);
    // Measured against the viewport, never a percentage. A percentage here is a
    // percentage of the scrim's auto-sized grid row — a row sized by this very
    // box — so it is cyclic, resolves as `auto`, and silently takes the bound
    // with it: the dialog then grew to the length of the file it was showing.
    expect(rule).toMatch(/height:\s*calc\(100dvh - 2 \* var\(--alkSpace6\)\)/);
    expect(rule).toMatch(/max-height:\s*calc\(100dvh - 2 \* var\(--alkSpace6\)\)/);
    expect(rule).not.toMatch(/%/);
  });

  it("drops the card and takes the screen below 900px", () => {
    const narrow = narrowBlock();
    expect(narrow).not.toBe("");
    // The scrim's own gutter has to go too — a 24px frame around a preview is the
    // only space a narrow screen has. It is one value, so dropping it both pulls
    // the padding off the scrim and gives the dialog the whole viewport to bound
    // itself against.
    const scrim = ruleFor('.alk-modal-scrim:has(.alk-modal[data-size="full"])', narrow);
    expect(scrim).toMatch(/--alk-modal-gutter:\s*0px/);
    const surface = ruleFor('.alk-modal[data-size="full"]', narrow);
    expect(surface).not.toBeNull();
    // The viewport here too, for the same reason — with the scrim's padding gone
    // the dialog IS the screen, and a percentage of the row it sits in is not.
    expect(surface).toMatch(/height:\s*100dvh/);
    expect(surface).toMatch(/max-height:\s*100dvh/);
    expect(surface).toMatch(/border-radius:\s*0/);
  });

  it("leaves the other sizes as cards", () => {
    // The sheet rule is scoped to `full`; a confirm dialog must not go full-screen
    // on a narrow window.
    expect(narrowBlock()).not.toMatch(/data-size="(sm|md|lg|xl)"/);
    expect(ruleFor('.alk-modal[data-size="sm"]')).toMatch(/--alk-modal-w:\s*408px/);
  });
});

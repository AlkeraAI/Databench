// The composer keeps a name on its model chip beside the files pane.
//
// The composer folds its chips by its own width: the model is named (by its
// short name first, "Sonnet 5.5") only once the composer is at least the width
// of that rung. With the files pane open, the conversation column has a floor
// so the chip never drops to a bare glyph, and this pins that the floor, less the page's padding and a generous
// allowance for the dock's own chrome, still clears the rung. jsdom lays
// nothing out, so the check reads the sources the two numbers live in.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const WEB = process.cwd();
const UI_SRC = resolve(WEB, "../../packages/ui/src");
const read = (path: string): string => readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

/** The chat column's floor, as the page hands it to the split view. */
function columnFloor(): number {
  const page = readFileSync(resolve(WEB, "src/pages/workspace/chat/ChatPage.tsx"), "utf8");
  expect(page, "the chat column takes its floor from the constant").toMatch(
    /id: "chat", fill: true, min: CONVERSATION_MIN/,
  );
  const match = /export const CONVERSATION_MIN = (\d+);/.exec(page);
  expect(match, "the chat column declares its floor").not.toBeNull();
  return Number(match![1]);
}

/** The composer width at which the model chip first shows a name. */
function modelLabelRung(): number {
  const css = read(resolve(UI_SRC, "chat/composer/composer.css"));
  const match =
    /@container chat-composer \(min-width: (\d+)px\) \{\s*\.chat-root\s*\.chat-composer-ctl\[data-rail="model"\]\s*\.chat-composer-trigger__label \{\s*display: inline;/.exec(
      css,
    );
  expect(match, "the composer has a rung that shows the model's name").not.toBeNull();
  return Number(match![1]);
}

/** The page's padding around the surface, both sides. */
function surfacePadding(): number {
  const tokens = read(resolve(UI_SRC, "theme/tokens.css"));
  const space = /--alkSpace7:\s*(\d+)px/.exec(tokens);
  const page = read(resolve(WEB, "src/pages/workspace/chat/chat-page.css"));
  expect(page).toMatch(/\.chat-page__surface \{[^}]*padding: var\(--alkSpace7\)/);
  return 2 * Number(space![1]);
}

/** More than the dock's padding and the composer unit's border take, both sides. */
const DOCK_ALLOWANCE = 2 * 48;

describe("the conversation column's floor", () => {
  it("leaves the composer wide enough to name the model", () => {
    expect(columnFloor() - surfacePadding() - DOCK_ALLOWANCE).toBeGreaterThanOrEqual(modelLabelRung());
  });
});

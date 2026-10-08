// The plan card's keycaps read in their own button's ink. jsdom applies no
// cascade from a sheet it never loaded, so the contract is read off the sheet:
// the rule that skins each key names `currentcolor` for its ink and derives any
// fill or edge from it, never from a fixed token that ignores the button's fill.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const css = readFileSync(resolve(process.cwd(), "../../packages/ui/src/chat/plan/plan.css"), "utf8");

function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`${escaped}\\s*\\{([^}]*)\\}`).exec(css);
  if (!match?.[1]) throw new Error(`no rule for ${selector}`);
  return match[1];
}

function declaration(body: string, property: string): string | undefined {
  return new RegExp(`(?:^|;|\\s)${property}\\s*:\\s*([^;]+);`).exec(body)?.[1]?.trim();
}

describe("plan card keycap ink", () => {
  it("draws the Enter key on the filled button in that button's own ink", () => {
    const body = ruleBody(".chat-root .chat-plan-approve .chat-plan-key");
    expect(declaration(body, "color")?.toLowerCase()).toBe("currentcolor");
    const background = declaration(body, "background") ?? "";
    expect(background.toLowerCase()).toMatch(/color-mix\(in srgb, currentcolor \d+%, transparent\)/);
    const border = declaration(body, "border") ?? "";
    expect(border.toLowerCase()).toMatch(/^1px solid color-mix\(in srgb, currentcolor \d+%, transparent\)$/);
    // No fixed ink: a token names one colour whatever the mode fill under it.
    expect(body).not.toMatch(/var\(--/);
  });

  it("keeps the Esc key on the outline button in that button's ink", () => {
    const body = ruleBody(".chat-root .chat-plan-reject .chat-plan-key");
    expect(declaration(body, "color")?.toLowerCase()).toBe("currentcolor");
  });
});

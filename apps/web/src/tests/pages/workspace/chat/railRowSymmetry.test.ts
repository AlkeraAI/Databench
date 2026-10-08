// The rounded rectangle a chat is drawn in rests the same distance from both
// edges of the rail.
//
// It did not. The key onto the row's menu stood BESIDE the name in the same
// flex line, so it held its own width open even while it was invisible, and
// every rectangle in the rail stopped a control's width short of the right edge
// while starting flush with the left. The rail read as a column of names that
// had all slid left, and the only thing accounting for the gap was a button
// nobody could see until they aimed at it.
//
// The fix is geometry, not markup: the key comes out of flow and is laid over
// the row's trailing end, and the row makes room for it only while it is
// actually up. None of that is visible to a render assertion, since jsdom loads
// no stylesheet and computes no layout, so the contract is read off the sheet
// itself, the way the chat chrome's token gate is.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const SHEET = join(process.cwd(), "src/pages/workspace/chat/chat-page.css");

interface Rule {
  selector: string;
  body: string;
}

/** Every `selector { … }` pair in the sheet, comments stripped and whitespace
 *  flattened. An at-rule's own header never matches, so the rules nested inside
 *  one are read at the same level as the rest. */
function rules(): Rule[] {
  const css = readFileSync(SHEET, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  return [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((match) => ({
    selector: match[1]!.trim().replace(/\s+/g, " "),
    body: match[2]!.trim(),
  }));
}

/** The one rule written for exactly this selector list. */
function only(selector: string): string {
  const found = rules().filter((rule) => rule.selector === selector);
  expect(found, `expected exactly one rule for \`${selector}\``).toHaveLength(1);
  return found[0]!.body;
}

/** Every rule that styles this element itself: the class in the LAST compound
 *  of one of its selectors, so `… :hover .chat-page__row` counts and
 *  `.chat-page__row-title` does not. */
function targeting(selector: string): Rule[] {
  const own = new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![\\w-])`);
  return rules().filter((rule) =>
    rule.selector.split(",").some((part) => {
      const last =
        part
          .trim()
          .split(/[\s>+~]+/)
          .pop() ?? "";
      return own.test(last);
    }),
  );
}

/** A declaration's value, or `null` if the body never sets it. */
function declared(body: string, property: string): string | null {
  const found = [...body.matchAll(/(-{0,2}[a-z][a-z-]*)\s*:\s*([^;]+);?/g)].filter(
    (match) => match[1] === property,
  );
  return found.length === 0 ? null : found[found.length - 1]![2]!.trim();
}

/** A shorthand's space-separated values, with `calc(a + b)` kept whole. */
function values(text: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let current = "";
  for (const char of text.trim()) {
    if (char === "(") depth += 1;
    if (char === ")") depth -= 1;
    if (depth === 0 && /\s/.test(char)) {
      if (current !== "") out.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  if (current !== "") out.push(current);
  return out;
}

/** What a rule leaves on each inline edge, folding the shorthand into the
 *  longhands the way the cascade does. */
function inlineInsets(body: string): { start: string | null; end: string | null } {
  let start: string | null = null;
  let end: string | null = null;
  for (const match of body.matchAll(/(padding[a-z-]*)\s*:\s*([^;]+);?/g)) {
    const property = match[1]!;
    const parts = values(match[2]!);
    if (property === "padding") {
      end = parts[1] ?? parts[0]!;
      start = parts[3] ?? parts[1] ?? parts[0]!;
    } else if (property === "padding-inline") {
      start = parts[0]!;
      end = parts[1] ?? parts[0]!;
    } else if (property === "padding-left" || property === "padding-inline-start") {
      start = parts.join(" ");
    } else if (property === "padding-right" || property === "padding-inline-end") {
      end = parts.join(" ");
    }
  }
  return { start, end };
}

/** The states in which the key is reached: pointer, focus, or its own menu open. */
const REACHED = [":hover", ":focus-within", "[data-open]"];
const reaches = (selector: string): boolean => REACHED.some((state) => selector.includes(state));

describe("the rail's rounded rectangle is symmetric at rest", () => {
  it("leaves the same inset on both inline edges", () => {
    const { start, end } = inlineInsets(only(".chat-page__row"));
    expect(start).not.toBeNull();
    expect(start).toBe(end);
  });

  it("spans the whole line, so the rectangle reaches the rail's own edges", () => {
    const body = only(".chat-page__row");
    expect(declared(body, "flex")).toBe("1 1 auto");
    expect(declared(body, "min-width")).toBe("0");
  });

  it("makes no trailing room for the key while the key is down", () => {
    // Every rule that pads the row's trailing edge has to earn it by naming a
    // state in which the key is actually showing.
    for (const rule of targeting(".chat-page__row")) {
      const { start, end } = inlineInsets(rule.body);
      if (end !== null && end !== start) {
        expect(reaches(rule.selector), `\`${rule.selector}\` pads the trailing edge`).toBe(true);
      }
    }
  });
});

describe("the key is laid over the row rather than beside it", () => {
  it("is out of flow, anchored to the line the name is on", () => {
    const key = only(".chat-page__row-menu");
    expect(declared(key, "position")).toBe("absolute");
    expect(declared(key, "inset-inline-end")).not.toBeNull();
    // Nothing that would take width back out of the line it is laid over.
    expect(declared(key, "flex")).toBeNull();
    expect(declared(key, "margin-left")).toBeNull();
    expect(declared(key, "margin-inline-start")).toBeNull();
    // The line, not the wrap: the wrap also holds the refusal notice, and
    // centring against that would drift the key downward the moment a rename is
    // refused.
    expect(declared(only(".chat-page__row-line"), "position")).toBe("relative");
  });

  it("takes no clicks while it is invisible, and takes them again once it is up", () => {
    expect(declared(only(".chat-page__row-menu"), "pointer-events")).toBe("none");
    const shown = targeting(".chat-page__row-menu").filter(
      (rule) => declared(rule.body, "opacity") === "1",
    );
    expect(shown).toHaveLength(1);
    expect(declared(shown[0]!.body, "pointer-events")).toBe("auto");
    // Pointer, keyboard focus and the open menu all count as reached. Dropping
    // focus-within would leave the key invisible under the tab that reached it.
    for (const state of REACHED) expect(shown[0]!.selector).toContain(state);
  });
});

describe("the room the row makes is exactly the key's width", () => {
  it("is spent from one declared width, while the key is up", () => {
    const size = declared(only(".chat-page__row-line"), "--chat-row-key");
    expect(size).not.toBeNull();
    expect(declared(only(".chat-page__row-menu"), "width")).toBe("var(--chat-row-key)");

    const made = targeting(".chat-page__row").filter(
      (rule) => inlineInsets(rule.body).end !== inlineInsets(rule.body).start,
    );
    expect(made).toHaveLength(1);
    expect(reaches(made[0]!.selector)).toBe(true);
    expect(inlineInsets(made[0]!.body).end).toContain("var(--chat-row-key)");
  });
});

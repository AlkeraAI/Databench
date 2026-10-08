// The permission-mode menu's own height.
//
// The menu lists a fixed five stances — each a label over a clamped two-line
// gloss — and closes with the Shift+Tab hint. That is about 450px of content,
// and the sheet capped it at 420: a scrollbar appeared beside a list that had
// nothing more to show, on a window with hundreds of spare pixels above it. A
// hand-picked pixel cap set near the content it holds is always one wording
// change away from this, so the menu is sized by what it lists and capped only
// by the window — and when the window IS too short, the rows scroll and the
// hint stays put rather than riding out of view with them.
//
// jsdom lays nothing out and applies no stylesheet, so the two halves are pinned
// separately: which element the browser will scroll (the DOM the component
// builds), and how much room the sheet gives it (the arithmetic a browser would
// do, from the sheet's own declarations).

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer } from "./Composer";

/** The five stances the product ships, with the glosses it ships them with —
 *  the content the menu actually has to hold. */
const MODES = [
  {
    value: "default",
    label: "Default",
    description: "Runs reads freely; asks before changes outside the chat's files.",
  },
  {
    value: "plan",
    label: "Plan",
    description: "Explores, writes only in the chat's files, and proposes a plan.",
  },
  {
    value: "auto",
    label: "Auto",
    description: "Works on its own and pauses only for risky or destructive steps.",
  },
  {
    value: "read_only",
    label: "Read-only",
    description: "Blocks every change outside this chat.",
  },
  {
    value: "bypass",
    label: "Bypass permissions",
    description: "Runs everything without asking.",
  },
];

const HINT = "Shift+Tab cycles modes";

afterEach(cleanup);

function openModeMenu(): HTMLElement {
  render(
    <div className="chat-root">
      <Composer
        modes={MODES}
        mode="bypass"
        onModeChange={vi.fn()}
        models={[{ value: "m1", label: "M1" }]}
        model="m1"
        onModelChange={vi.fn()}
        efforts={[{ value: "low", label: "Low", bars: 1 as const }]}
        effort="low"
        onEffortChange={vi.fn()}
        onSend={vi.fn()}
      />
    </div>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Permission mode: Bypass permissions" }));
  const pop = screen.getByRole("listbox", { name: "Permission mode" }).closest(".chat-composer-pop");
  if (!(pop instanceof HTMLElement)) throw new Error("the mode menu did not open");
  return pop;
}

describe("what the permission-mode menu scrolls", () => {
  it("scrolls the stances and leaves the Shift+Tab hint standing under them", () => {
    const pop = openModeMenu();
    const scroller = pop.querySelector(".chat-composer-pop__scroll");
    if (!(scroller instanceof HTMLElement)) {
      throw new Error("the menu has no scroll region distinct from the menu itself");
    }
    // Every stance is in the part that moves…
    expect(scroller.contains(within(pop).getByRole("listbox", { name: "Permission mode" }))).toBe(
      true,
    );
    // …and the footer is not, or a short window scrolls away the one line that
    // says how to cycle the modes without opening this at all.
    expect(scroller.contains(within(pop).getByText(HINT))).toBe(false);
  });
});

describe("the room the sheet gives the permission-mode menu", () => {
  const HERE = dirname(fileURLToPath(import.meta.url));
  const CSS = readFileSync(join(HERE, "composer.css"), "utf8");
  const TOKENS = readFileSync(join(HERE, "..", "theme", "tokens.css"), "utf8");

  /** Comments go first: they carry braces of their own and would split a rule. */
  const RULES = CSS.replace(/\/\*[\s\S]*?\*\//g, "");

  function rule(selector: string): Map<string, string> {
    const blocks = [...RULES.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(
      (m) => (m[1] ?? "").split("}").at(-1)?.trim() === selector,
    );
    const body = blocks.at(-1)?.[2];
    if (body === undefined) throw new Error(`no rule for ${selector}`);
    const declarations = new Map<string, string>();
    for (const line of body.split(";")) {
      const at = line.indexOf(":");
      if (at === -1) continue;
      declarations.set(line.slice(0, at).trim(), line.slice(at + 1).trim());
    }
    return declarations;
  }

  const pop = rule(".chat-root .chat-composer-pop");
  const modePop = rule('.chat-root .chat-composer-ctl[data-rail="mode"] .chat-composer-pop');
  const option = rule(".chat-root .chat-composer-opt");
  const label = rule(".chat-root .chat-composer-opt__label");
  const gloss = rule(".chat-root .chat-composer-opt__desc");
  const hint = rule(".chat-root .chat-composer-pop__hint");

  /** Token values this geometry is spent in, read from the sheets that declare
   *  them — the popover's own locals first, then the token layer. */
  const tokens = new Map(
    [...TOKENS.matchAll(/(--chat-[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [
      m[1] as string,
      (m[2] ?? "").trim(),
    ]),
  );

  function resolveVars(value: string): string {
    return value.replace(
      /var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*))?\)/g,
      (_all, name: string, fallback?: string) => {
        const declared = pop.get(name) ?? tokens.get(name) ?? fallback;
        if (declared === undefined) throw new Error(`undeclared ${name}`);
        return declared;
      },
    );
  }

  /** Split on a separator that is not inside parentheses. */
  function splitTop(text: string, separator: string): string[] {
    const parts: string[] = [];
    let depth = 0;
    let current = "";
    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i] as string;
      if (ch === "(") depth += 1;
      if (ch === ")") depth -= 1;
      if (depth === 0 && text.startsWith(separator, i)) {
        parts.push(current);
        current = "";
        i += separator.length - 1;
        continue;
      }
      current += ch;
    }
    parts.push(current);
    return parts;
  }

  /** A CSS length in px, with `100vh` answered by the window this test poses.
   *  Handles the three forms these rules are written in: a bare length, a
   *  `min()`/`max()` of them, and `calc(a - b)`. */
  function lengthPx(raw: string, viewport: number): number {
    const text = resolveVars(raw).trim();
    const call = /^(min|max|calc)\(([\s\S]*)\)$/.exec(text);
    if (call) {
      const inner = call[2] as string;
      const of = (parts: string[]): number[] => parts.map((p) => lengthPx(p, viewport));
      if (call[1] === "min") return Math.min(...of(splitTop(inner, ",")));
      if (call[1] === "max") return Math.max(...of(splitTop(inner, ",")));
      const minus = splitTop(inner, " - ");
      if (minus.length > 1) return of(minus).reduce((a, b) => a - b);
      return of(splitTop(inner, " + ")).reduce((a, b) => a + b);
    }
    const viewportUnit = /^([\d.]+)vh$/.exec(text);
    if (viewportUnit) return (Number(viewportUnit[1]) / 100) * viewport;
    const px = /^([\d.]+)px$/.exec(text);
    if (px) return Number(px[1]);
    throw new Error(`not a length this test reads: ${raw}`);
  }

  /** The first value of a shorthand — the TOP of a `margin` or a `padding`. */
  const first = (shorthand: string | undefined): string =>
    /^(calc\(.*?\)\)|\S+)/.exec((shorthand ?? "").trim())?.[0] ?? "";

  const len = (value: string | undefined, viewport = 0): number => {
    if (value === undefined) throw new Error("no such declaration");
    return lengthPx(value, viewport);
  };

  /** One stance: a label over its gloss, clamped to the sheet's own line count,
   *  inside the row's air. */
  const rowHeight =
    2 * len(first(option.get("padding"))) +
    len(label.get("line-height")) +
    len(option.get("row-gap")) +
    Number(gloss.get("line-clamp")) * len(gloss.get("line-height"));

  /** The hint under them: its air, the rule over it, and its one line. */
  const hintHeight =
    len(first(hint.get("margin"))) +
    len(hint.get("padding-top")) +
    len(first(hint.get("border-top"))) +
    len(hint.get("line-height"));

  /** Everything the menu has to hold at the widths where the folded model and
   *  effort groups are off (which is every width above the squeeze rung). */
  const content = MODES.length * rowHeight + hintHeight + 2 * len(pop.get("padding"));

  /** The cap the sheet puts on the mode menu in a window this tall. */
  const cap = (viewport: number): number => len(modePop.get("max-height"), viewport);

  it("holds all five stances and the hint in an ordinary window", () => {
    // A 900px window — a laptop with the browser chrome and a dock on — has
    // hundreds of pixels to spare above the composer. Nothing may scroll here.
    expect(content).toBeGreaterThan(0);
    expect(cap(900)).toBeGreaterThanOrEqual(content);
  });

  it("yields to a window genuinely too short to hold it", () => {
    // The cap is not gone, it is the window's: a 480px window cannot show this
    // menu whole, so it scrolls rather than reaching past the panel's top edge.
    expect(cap(480)).toBeLessThan(content);
    expect(cap(480)).toBeGreaterThan(0);
  });

  it("scrolls the rows and not the menu around them", () => {
    // The cap is on the menu; the overflow is on the region inside it that
    // holds the rows, which is what keeps the hint out of the scroll.
    expect(pop.get("overflow-y")).toBeUndefined();
    expect(rule(".chat-root .chat-composer-pop__scroll").get("overflow-y")).toBe("auto");
  });
});

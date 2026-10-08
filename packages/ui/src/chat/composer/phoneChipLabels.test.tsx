// On a phone the model chip says which model, and no chip is cut to a stub.
//
// At 390 px the composer is a little over 300 px wide. The chips once stood as
// two glyphs nobody could tell apart, then as labels cut to "Claud…" and "M.",
// which said no more. A phone's rung now gives the model chip its short name
// ("Sonnet 5.5", the family word dropped) and leaves effort to its bars, which
// read its level; the wider rungs still give both their full labels. jsdom lays
// nothing out, so the check reads the sheet's ladder, and the chip's words are
// read off the rendered composer.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Composer } from "./Composer";
import { shortModelLabel } from "./modelLabels";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "composer.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

/** A 390 px phone, less the page's 16 px padding each side and the dock's own. */
const PHONE_COMPOSER_PX = 390 - 2 * 16 - 2 * 20;

/** Every container rung, by its width, with its body. */
function rungs(): { at: number; body: string }[] {
  const found: { at: number; body: string }[] = [];
  const re = /@container chat-composer \(min-width: (\d+)px\) \{/g;
  for (let m = re.exec(css); m !== null; m = re.exec(css)) {
    let depth = 1;
    let i = m.index + m[0].length;
    while (depth > 0 && i < css.length) {
      if (css[i] === "{") depth += 1;
      if (css[i] === "}") depth -= 1;
      i += 1;
    }
    found.push({ at: Number(m[1]), body: css.slice(m.index + m[0].length, i - 1) });
  }
  return found;
}

/** Every rule in `body` naming `part` of the chip, joined. */
function chipRule(body: string, chip: "model" | "effort", part: "label" | "narrow" | "wide"): string | null {
  const re = new RegExp(
    `\\.chat-composer-ctl\\[data-rail="${chip}"\\]\\s*\\.chat-composer-trigger__${part}[^{]*\\{([^}]*)\\}`,
    "g",
  );
  const bodies = [...body.matchAll(re)].map((m) => m[1]);
  return bodies.length === 0 ? null : bodies.join("\n");
}

const onPhone = (): { at: number; body: string }[] => rungs().filter((rung) => rung.at <= PHONE_COMPOSER_PX);
const wider = (): { at: number; body: string }[] => rungs().filter((rung) => rung.at > PHONE_COMPOSER_PX);

describe("the model and effort chips on a phone", () => {
  it("show the model's short name there, and its full name on the wider rungs", () => {
    const phone = onPhone();
    expect(phone.some((rung) => /display: inline/.test(chipRule(rung.body, "model", "label") ?? ""))).toBe(true);
    expect(phone.some((rung) => /display: inline/.test(chipRule(rung.body, "model", "narrow") ?? ""))).toBe(true);
    expect(phone.some((rung) => /display: inline/.test(chipRule(rung.body, "model", "wide") ?? ""))).toBe(false);
    expect(wider().some((rung) => /display: inline/.test(chipRule(rung.body, "model", "wide") ?? ""))).toBe(true);
    expect(wider().some((rung) => /display: none/.test(chipRule(rung.body, "model", "narrow") ?? ""))).toBe(true);
  });

  it("never cut the effort label to a stub: it shows whole on a wider rung or not at all", () => {
    for (const rung of onPhone()) {
      expect(chipRule(rung.body, "effort", "label"), `effort label at ${rung.at}px`).toBeNull();
    }
    expect(wider().some((rung) => /max-width: none/.test(chipRule(rung.body, "effort", "label") ?? ""))).toBe(true);
  });

  it("keep the mode chip's compact word to the mode chip", () => {
    for (const rung of rungs()) {
      expect(rung.body, `rung ${rung.at}px`).not.toMatch(/(^|[\s,}])\.chat-root \.chat-composer-trigger__(narrow|wide)/);
    }
  });

  it("put both names in the model chip, the short one for the phone rung", () => {
    render(
      <Composer
        modes={[{ value: "ask", label: "Ask" }]}
        mode="ask"
        onSend={() => {}}
        models={[{ value: "claude-sonnet-5.5", label: "Claude Sonnet 5.5" }]}
        model="claude-sonnet-5.5"
        onModelChange={() => {}}
        efforts={[{ value: "medium", label: "Medium", bars: 2 }]}
        effort="medium"
        onEffortChange={() => {}}
      />,
    );
    const chip = screen.getByRole("button", { name: /^Model: / });
    expect(chip.querySelector(".chat-composer-trigger__narrow")?.textContent).toBe("Sonnet 5.5");
    expect(chip.querySelector(".chat-composer-trigger__wide")?.textContent).toBe("Claude Sonnet 5.5");
  });
});

describe("shortModelLabel", () => {
  it.each([
    ["Claude Sonnet 5.5", "Sonnet 5.5"],
    ["Claude Haiku 4.5", "Haiku 4.5"],
    ["GPT-5.5", undefined],
    ["GPT-5.5 mini", undefined],
    ["Claude", undefined],
    ["Claudette 2", undefined],
  ])("%s → %s", (label, short) => {
    expect(shortModelLabel(label)).toBe(short);
  });
});

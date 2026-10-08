// The panel input is styled in one place, and it grows along a row only: a
// flex basis on the input itself became its HEIGHT in a panel's column stack
// (the Variables filter rendered 140 px tall).

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "../..");

function cssFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return cssFiles(path);
    return name.endsWith(".css") ? [path] : [];
  });
}

/** Bodies of every rule whose selector is exactly `selector`. */
function baseRules(css: string, selector: string): string[] {
  const escaped = selector.replace(/[.]/g, "\\.");
  return [
    ...css.matchAll(new RegExp(`(^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`, "g")),
  ].map((m) => m[2]);
}
const baseInputRules = (css: string): string[] => baseRules(css, ".nb-input");

describe("the panel input style", () => {
  const rules = cssFiles(SRC).flatMap((path) =>
    baseInputRules(readFileSync(path, "utf-8")),
  );

  it("is defined once", () => {
    expect(rules).toHaveLength(1);
  });

  it("carries no flex sizing of its own, so a column never stretches it", () => {
    expect(rules[0]).not.toMatch(/\bflex(-basis|-grow)?\s*:/);
  });
});

describe("a panel's content style", () => {
  it("is defined once, so the dock a panel opens in cannot restyle it", () => {
    // The dock once shared `.nb-panel`, which gave every panel's content the
    // dock's fixed 320 px width and border: a narrow inner box that cut off.
    const rules = cssFiles(SRC).flatMap((path) =>
      baseRules(readFileSync(path, "utf-8"), ".nb-panel"),
    );
    expect(rules).toHaveLength(1);
  });
});

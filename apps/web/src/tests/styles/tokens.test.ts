import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

// The portal's twin of `packages/ui/src/theme/tokens.test.ts`.
//
// The library lints the token references in its OWN sheets, and nothing linted the app's. That gap
// is not theoretical: the session notice shipped `background: var(--alkWarningBg, var(--alkSurfaceMuted))`
// and neither name exists anywhere in the repo, so the declaration was invalid at computed-value
// time and the strip painted no background at all. jsdom neither cascades nor computes custom
// properties, so no render test can see it — reading the sheets is the only way it is caught.
//
// The token layer is the library's, so the set of names that ARE defined is read from
// `packages/ui/src`; the app may also define its own, and both count.

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB_SRC = resolve(HERE, "../..");
const UI_SRC = resolve(HERE, "../../../../../packages/ui/src");

function sheets(dir: string, found: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) sheets(path, found);
    else if (entry.name.endsWith(".css")) found.push(path);
  }
  return found;
}

const WEB_SHEETS = sheets(WEB_SRC).sort();
const UI_SHEETS = sheets(UI_SRC).sort();

/** Every `--alk*` custom property assigned in CSS anywhere the portal's bundle draws from. */
const ASSIGNED: ReadonlySet<string> = new Set(
  [...UI_SHEETS, ...WEB_SHEETS].flatMap((sheet) =>
    Array.from(
      readFileSync(sheet, "utf8").matchAll(/(--alk[A-Za-z0-9_-]*)\s*:/g),
      (m) => m[1] ?? "",
    ),
  ),
);

/** Every `--alk*` a component writes as an inline style — a knob set per instance, in TS. */
function sources(dir: string, found: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) sources(path, found);
    else if (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx")) found.push(path);
  }
  return found;
}

const WRITTEN_BY_A_COMPONENT: ReadonlySet<string> = new Set(
  [...sources(WEB_SRC), ...sources(UI_SRC)].flatMap((source) =>
    Array.from(
      readFileSync(source, "utf8").matchAll(/["'](--alk[A-Za-z0-9_-]*)["']\s*[\]:]/g),
      (m) => m[1] ?? "",
    ),
  ),
);

const defined = (token: string): boolean =>
  ASSIGNED.has(token) || WRITTEN_BY_A_COMPONENT.has(token);

/** A theme token carries the scheme (`--alkCamelCase`); a per-instance knob is kebab. */
const isThemeToken = (token: string): boolean => /^--alk[A-Z]/.test(token);

interface Reference {
  where: string;
  token: string;
  fallback: string;
  declaration: string;
}

/** The fallback of the `var(` whose token ends at `from`, read to its matching paren — so
 *  `rgb(0 0 0 / 12%)` arrives whole, and a NESTED `var(--other)` is left in place for the scan's
 *  own next match to find. An undefined token hiding inside another's fallback is exactly how the
 *  session notice shipped, so the nested one has to be a reference in its own right. */
function fallbackAt(line: string, from: number): string {
  let depth = 1;
  for (let i = from; i < line.length; i += 1) {
    const ch = line[i];
    if (ch === "(") depth += 1;
    else if (ch === ")") {
      depth -= 1;
      if (depth === 0) return line.slice(from, i);
    }
  }
  return line.slice(from);
}

function referencesInText(text: string, where: string): Reference[] {
  const found: Reference[] = [];
  text.split("\n").forEach((line, index) => {
    for (const match of line.matchAll(/var\(\s*(--alk[A-Za-z0-9_-]*)\s*(,?)/g)) {
      const after = (match.index ?? 0) + match[0].length;
      found.push({
        where: `${where}:${index + 1}`,
        token: match[1] ?? "",
        fallback: match[2] === "," ? fallbackAt(line, after).trim() : "",
        declaration: line.trim(),
      });
    }
  });
  return found;
}

function referencesIn(sheet: string): Reference[] {
  return referencesInText(
    readFileSync(sheet, "utf8"),
    relative(WEB_SRC, sheet).split("\\").join("/"),
  );
}

const REFERENCES = WEB_SHEETS.flatMap(referencesIn);
const report = (refs: Reference[]): string[] => refs.map((ref) => `${ref.where} ${ref.declaration}`);

describe("the tokens the portal's stylesheets reach for", () => {
  it("scans the whole app, so a new sheet is covered by being written", () => {
    expect(WEB_SHEETS.length).toBeGreaterThan(10);
    expect(ASSIGNED.size).toBeGreaterThan(100);
    expect(REFERENCES.length).toBeGreaterThan(100);
  });

  it("names only theme tokens something defines", () => {
    // A name nothing assigns has no value in either scheme: the declaration is void without a
    // fallback, and frozen on one appearance with one.
    const dangling = REFERENCES.filter((ref) => isThemeToken(ref.token) && !defined(ref.token));

    expect(report(dangling)).toEqual([]);
  });

  it("gives every per-instance knob a default, since nothing may be there to set it", () => {
    const bare = REFERENCES.filter(
      (ref) => !isThemeToken(ref.token) && !defined(ref.token) && ref.fallback === "",
    );

    expect(report(bare)).toEqual([]);
  });

  it("would catch a name nobody assigns, including one hiding behind another", () => {
    // The scan is only worth having if it fails on what it forbids. The shape that shipped is the
    // second line: a dangling token whose fallback is a second dangling token, which reads as
    // deliberate and paints nothing.
    const invented = referencesInText(
      [
        "  color: var(--alkNoSuchToken);",
        "  background: var(--alkAlsoMissing, var(--alkStillMissing));",
        "  border-color: var(--alkPrimaryText);",
      ].join("\n"),
      "invented.css",
    );
    const dangling = invented.filter((ref) => isThemeToken(ref.token) && !defined(ref.token));

    expect(dangling.map((ref) => ref.token)).toEqual([
      "--alkNoSuchToken",
      "--alkAlsoMissing",
      "--alkStillMissing",
    ]);
    // ...and stays quiet on a name the theme really assigns.
    expect(defined("--alkPrimaryText")).toBe(true);
  });
});

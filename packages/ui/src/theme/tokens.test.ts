import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

// The token layer is the only thing in this library that has a light value and a dark one. A
// stylesheet that reaches past it is a stylesheet that paints the same pixel in both themes, and
// jsdom neither cascades nor computes custom properties — so the ways a sheet can reach past it
// are caught by reading the sheets, which is also how the chat pane's layout contracts are checked.
//
// Two families of `--alk` custom property live here, and they fail differently:
//
//  * a THEME TOKEN (`--alkCamelCase`) carries the scheme. It must be one this library assigns —
//    a name nothing assigns has no value in either scheme, so the declaration is either void
//    (no fallback: invalid at computed-value time, the property is simply not set) or frozen on
//    whatever the fallback says, which is one appearance on both grounds.
//  * a PER-INSTANCE KNOB (`--alk-kebab-case`) is a parameter a component or a host page writes at
//    runtime — the series colour, the clamp line count, an entrance's travel. It is legitimately
//    unassigned in CSS, so what it owes is a default: a `var()` with no fallback and no assignment
//    renders nothing when the caller leaves the knob alone.
//
// A knob whose name is a kebab spelling of a real token is neither: it is a typo for the token,
// and it silently paints its fallback while the theme value it meant to read sits unused.

// Vitest runs this package from its own root, which is what the sheets are named relative to in
// the reports below.
const UI_SRC = join(process.cwd(), "src");

function files(dir: string, ext: string, found: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) files(path, ext, found);
    else if (entry.name.endsWith(ext)) found.push(path);
  }
  return found;
}

const SHEETS = files(UI_SRC, ".css").sort();
const SOURCES = [...files(UI_SRC, ".ts"), ...files(UI_SRC, ".tsx")].sort();
const named = (path: string): string => relative(UI_SRC, path).split("\\").join("/");

/** Every `--alk*` custom property this library assigns in its own CSS. */
const ASSIGNED_IN_CSS: ReadonlySet<string> = new Set(
  SHEETS.flatMap((sheet) =>
    Array.from(readFileSync(sheet, "utf8").matchAll(/(--alk[A-Za-z0-9_-]*)\s*:/g), (m) => m[1] ?? ""),
  ),
);

/** Every `--alk*` a component writes as an inline style — a knob set per instance, in TS. */
const WRITTEN_BY_A_COMPONENT: ReadonlySet<string> = new Set(
  SOURCES.flatMap((source) =>
    Array.from(readFileSync(source, "utf8").matchAll(/["'](--alk[A-Za-z0-9_-]*)["']\s*[\]:]/g), (m) => m[1] ?? ""),
  ),
);

const DEFINED = (token: string): boolean =>
  ASSIGNED_IN_CSS.has(token) || WRITTEN_BY_A_COMPONENT.has(token);

/** A theme token carries the scheme; everything else is a per-instance knob. */
const isThemeToken = (token: string): boolean => /^--alk[A-Z]/.test(token);

/** `--alk-danger-text` → `--alkDangerText`: the token a kebab knob would be a typo for. */
function asThemeToken(knob: string): string | null {
  if (!knob.startsWith("--alk-")) return null;
  const parts = knob.slice("--alk-".length).split("-").filter(Boolean);
  return `--alk${parts.map((part) => part[0]?.toUpperCase() + part.slice(1)).join("")}`;
}

/** One `var(--alk…)` reference, with where it is and what it falls back to. */
interface Reference {
  where: string;
  token: string;
  fallback: string;
  declaration: string;
}

function referencesIn(sheet: string): Reference[] {
  const found: Reference[] = [];
  readFileSync(sheet, "utf8")
    .split("\n")
    .forEach((line, index) => {
      // The fallback is read to the matching paren so `rgb(0 0 0 / 12%)` and a nested
      // `var(--other)` both arrive whole.
      for (const match of line.matchAll(/var\(\s*(--alk[A-Za-z0-9_-]*)\s*(?:,([^;]*))?\)/g)) {
        found.push({
          where: `${named(sheet)}:${index + 1}`,
          token: match[1] ?? "",
          fallback: (match[2] ?? "").trim(),
          declaration: line.trim(),
        });
      }
    });
  return found;
}

const REFERENCES = SHEETS.flatMap(referencesIn);
const report = (refs: Reference[]): string[] => refs.map((ref) => `${ref.where} ${ref.declaration}`);

/** A value that names a colour outright, rather than deferring to the theme. */
const LITERAL_COLOUR = /^(#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\(|white$|black$)/;
/** The declarations that can carry a colour — the shorthands included, since `border: 1px solid
 *  var(…)` freezes a hue just as thoroughly as `border-color` does. */
const PAINTS = /^\s*(color|background|border|outline|box-shadow|text-decoration|fill|stroke)[a-z-]*\s*:/;

describe("the tokens this library's own stylesheets reach for", () => {
  it("scans the whole library, so a new sheet is covered by being written", () => {
    expect(SHEETS.length).toBeGreaterThan(100);
    expect(SOURCES.length).toBeGreaterThan(100);
    expect(ASSIGNED_IN_CSS.size).toBeGreaterThan(100);
    expect(REFERENCES.length).toBeGreaterThan(1000);
  });

  it("names only theme tokens this library defines", () => {
    const dangling = REFERENCES.filter((ref) => isThemeToken(ref.token) && !DEFINED(ref.token));

    expect(report(dangling)).toEqual([]);
  });

  it("gives every per-instance knob a default, since nothing may be there to set it", () => {
    const bare = REFERENCES.filter(
      (ref) => !isThemeToken(ref.token) && !DEFINED(ref.token) && ref.fallback === "",
    );

    expect(report(bare)).toEqual([]);
  });

  it("never spells a theme token as a knob, which reads the fallback and not the theme", () => {
    const misspelt = REFERENCES.filter((ref) => {
      if (isThemeToken(ref.token)) return false;
      const token = asThemeToken(ref.token);
      return token != null && !DEFINED(ref.token) && ASSIGNED_IN_CSS.has(token);
    });

    expect(report(misspelt)).toEqual([]);
  });

  it("never paints from a literal colour behind a token it does not define", () => {
    const frozen = REFERENCES.filter(
      (ref) =>
        !DEFINED(ref.token) && LITERAL_COLOUR.test(ref.fallback) && PAINTS.test(ref.declaration),
    );

    expect(report(frozen)).toEqual([]);
  });
});

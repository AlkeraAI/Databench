// The notebook's stylesheets against the app's token layer.
//
// The notebook draws from its own `--nb-*` tokens, each of which reads one of
// @alkera/ui's `--alk*` tokens and falls back to a value of its own. A token
// name @alkera/ui does not define is not an error to a browser: the fallback
// is used, silently, and the notebook is drawn in its standalone light
// palette inside a dark app. So the names are held here: every `--alk*` the
// notebook reads is one the app defines, and the grounds and inks it stands
// on are defined for both colour schemes.

import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const REPO = resolve(__dirname, "../../../../../../../../..");
const NOTEBOOK_SRC = join(REPO, "packages/notebook-ui/src");
const TOKENS = readFileSync(join(REPO, "packages/ui/src/theme/tokens.css"), "utf-8");

function stylesheets(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return stylesheets(path);
    return entry.name.endsWith(".css") ? [path] : [];
  });
}

const withoutComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

/** Every custom property a stylesheet reads through `var(...)`. */
function reads(css: string): string[] {
  return [...withoutComments(css).matchAll(/var\(\s*(--[A-Za-z0-9_-]+)/g)].map((m) => m[1]!);
}

/** The custom properties a block of declarations defines, with their values. */
function declared(css: string): Map<string, string> {
  return new Map([...withoutComments(css).matchAll(/(--[A-Za-z0-9_-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1]!, m[2]!.trim()]));
}

// tokens.css opens with the dark scheme (the document default) and declares
// light under the scheme attribute.
const LIGHT_AT = TOKENS.indexOf(':root[data-alkera-color-scheme="light"]');
const DARK = declared(TOKENS.slice(0, LIGHT_AT));
const LIGHT = declared(TOKENS.slice(LIGHT_AT));

const SHEETS = stylesheets(NOTEBOOK_SRC);
const HOST_READS = [...new Set(SHEETS.flatMap((path) => reads(readFileSync(path, "utf-8"))).filter((name) => name.startsWith("--alk")))].sort();

describe("the notebook's stylesheets", () => {
  it("are found, and read the app's tokens", () => {
    expect(LIGHT_AT).toBeGreaterThan(0);
    expect(SHEETS.map((path) => path.slice(NOTEBOOK_SRC.length + 1)).sort()).toEqual([
      "components/dialogs/dialogs.css",
      "components/panels/panels.css",
      "editor/editor.css",
      "frame/frame.css",
      "outputs/outputs.css",
      "theme/notebook.css",
    ]);
    expect(HOST_READS.length).toBeGreaterThanOrEqual(15);
  });

  it.each(HOST_READS)("reads %s, which the app defines", (name) => {
    expect(DARK.has(name) || LIGHT.has(name)).toBe(true);
  });

  it.each(["--alkPageBg", "--alkCardBg", "--alkPrimaryText", "--alkSecondaryText", "--alkBorder", "--alkBorderStrong"])(
    "stands on %s, which has a value of its own in each colour scheme",
    (name) => {
      expect(HOST_READS).toContain(name);
      expect(DARK.get(name)).toBeDefined();
      expect(LIGHT.get(name)).toBeDefined();
      expect(DARK.get(name)).not.toBe(LIGHT.get(name));
    },
  );

  it("takes every colour from a token outside the token sheet itself", () => {
    const offenders = SHEETS.filter((path) => !path.endsWith(join("theme", "notebook.css"))).flatMap((path) =>
      withoutComments(readFileSync(path, "utf-8"))
        .split("\n")
        .map((line, index) => ({ line: line.trim(), where: `${path.slice(NOTEBOOK_SRC.length + 1)}:${index + 1}` }))
        // A colour literal that is not the fallback of a `var(...)`.
        .filter(({ line }) => /(#[0-9a-fA-F]{3,8}\b|rgba?\()/.test(line.replace(/var\([^()]*(\([^()]*\))?[^()]*\)/g, "")))
        .map(({ where, line }) => `${where} ${line}`),
    );
    expect(offenders).toEqual([]);
  });

  it("gives the standalone palette a dark value for every colour it has a light one for", () => {
    const sheet = withoutComments(readFileSync(join(NOTEBOOK_SRC, "theme/notebook.css"), "utf-8"));
    const darkAt = sheet.indexOf('.nb-root[data-nb-theme="dark"]');
    const mapAt = sheet.indexOf("--nb-bg:");
    expect(darkAt).toBeGreaterThan(0);
    expect(mapAt).toBeGreaterThan(darkAt);
    const own = (css: string) => [...declared(css).keys()].filter((name) => name.startsWith("--nb-own-")).sort();
    const light = own(sheet.slice(0, darkAt));
    expect(light.length).toBeGreaterThanOrEqual(15);
    expect(own(sheet.slice(darkAt, mapAt))).toEqual(light);
  });
});

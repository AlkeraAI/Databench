// Every stylesheet in this package is loaded by something.
//
// A stylesheet nobody imports ships nothing: its rules never reach the page, and
// edits to it look like they land while changing nothing on screen. That is how
// the reader's own message bubble lost its styling when a refactor dropped the
// one import of ticket.css. This test reads the package's sources and fails on
// any stylesheet that no script or stylesheet imports and the package does not
// export as an entry point for apps to load.

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "node_modules" ? [] : walk(path);
    return [path];
  });
}

const PACKAGE = JSON.parse(readFileSync(resolve(SRC, "..", "package.json"), "utf8")) as {
  exports?: Record<string, string>;
};
const exported = new Set(
  Object.values(PACKAGE.exports ?? {})
    .filter((target) => target.endsWith(".css"))
    .map((target) => resolve(SRC, "..", target)),
);

const files = walk(SRC);
const stylesheets = files.filter((f) => f.endsWith(".css"));
const importers = files.filter((f) => /\.(tsx?|css)$/.test(f) && !/\.test\.tsx?$/.test(f));

function imported(sheet: string): boolean {
  if (exported.has(sheet)) return true;
  return importers.some((file) => {
    if (file === sheet) return false;
    const text = readFileSync(file, "utf8");
    const wanted = relative(dirname(file), sheet).replaceAll("\\", "/");
    const spelled = wanted.startsWith(".") ? wanted : `./${wanted}`;
    return text.includes(`"${spelled}"`) || text.includes(`'${spelled}'`) || text.includes(`/${basename(sheet)}"`);
  });
}

describe("stylesheets", () => {
  it("finds the package's stylesheets", () => {
    expect(stylesheets.length).toBeGreaterThan(0);
  });

  it.each(stylesheets.map((sheet) => [relative(SRC, sheet)]))("%s is imported somewhere", (sheet) => {
    expect(imported(join(SRC, sheet))).toBe(true);
  });
});

// The converse: every relative @import names a stylesheet that exists. A move
// that carries a stylesheet to another folder without rewriting its @import
// fails the app build, which no other test of this package runs.
describe("relative @import", () => {
  const relativeImports = stylesheets.flatMap((sheet) =>
    [...readFileSync(sheet, "utf8").matchAll(/@import\s+(?:url\()?["'](\.{1,2}\/[^"']+)["']/g)].map(
      (match) => [relative(SRC, sheet), match[1]] as const,
    ),
  );

  it("finds the package's relative imports", () => {
    expect(relativeImports.length).toBeGreaterThan(5);
  });

  it.each(relativeImports)("%s imports %s, which exists", (sheet, target) => {
    expect(existsSync(resolve(SRC, dirname(sheet), target))).toBe(true);
  });
});

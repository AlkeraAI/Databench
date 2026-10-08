import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { describe, expect, it } from "vitest";

// Storage is quoted in decimal units everywhere — one formatter, one parser, and
// nothing else in the product that turns a storage byte count into a figure a
// person reads. This scan is what keeps that true: a second formatter with a
// 1024 divisor, or a "GiB" label, is how the dashboard and the admin page came
// to disagree about the same number in the first place, and a local helper that
// looks harmless is exactly the shape that brings it back.
//
// The same-shaped scan as `keys.test.ts`: it reads the tree rather than trusting
// a reviewer to notice.

const SRC = resolve(__dirname, "../../..");

/** Directories whose bytes are NOT storage a person reads: upload part sizing,
 *  hashing chunk sizes and the size-filter chips are all machine figures where
 *  a power of two is the right number. */
const EXEMPT = [
  join(SRC, "api", "blake3.ts"),
  join(SRC, "api", "blake3.worker.ts"),
  join(SRC, "api", "filesUpload.ts"),
  join(SRC, "pages", "workspace", "files", "filterState.ts"),
];

/** The one module allowed to spell a storage unit table. */
const FORMATTER = join(SRC, "lib", "format", "bytes.ts");

function* walk(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      if (entry === "tests" || entry === "node_modules") continue;
      yield* walk(path);
      continue;
    }
    if (/\.tsx?$/.test(path)) yield path;
  }
}

const SOURCES = [...walk(SRC)].filter((path) => path !== FORMATTER && !EXEMPT.includes(path));

describe("storage units", () => {
  it("has sources to scan (a broken walk would pass everything below)", () => {
    expect(SOURCES.length).toBeGreaterThan(100);
  });

  it("exempts only files that exist, so a stale entry cannot quietly widen the hole", () => {
    const all = new Set([...walk(SRC)]);
    expect(EXEMPT.filter((path) => !all.has(path))).toEqual([]);
    expect(all.has(FORMATTER)).toBe(true);
  });

  it("never labels a size in binary units", () => {
    const offenders = SOURCES.filter((path) => /\b(?:GiB|MiB|TiB|KiB|PiB)\b/.test(readFileSync(path, "utf8")))
      .map((path) => relative(SRC, path));
    expect(offenders).toEqual([]);
  });

  it("scales a size by 1024 in no module but the one formatter", () => {
    const offenders = SOURCES.filter((path) => {
      const source = readFileSync(path, "utf8");
      return /1024\s*\*\*\s*\d|\/\s*1024\b|\*\s*1024\s*\*\*|>=\s*1024\b/.test(source);
    }).map((path) => relative(SRC, path));
    expect(offenders).toEqual([]);
  });

  it("keeps the formatter itself free of a binary divisor", () => {
    const source = readFileSync(FORMATTER, "utf8");
    // 1024 appears only in the comment naming what is NOT done any more.
    const code = source
      .split("\n")
      .filter((line) => !line.trimStart().startsWith("//") && !line.trimStart().startsWith("*"))
      .join("\n");
    expect(code).not.toMatch(/1024/);
    expect(code).not.toMatch(/GiB|MiB|TiB/);
  });
});

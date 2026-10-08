// @vitest-environment node
// Every portal extension the open app defines is installed by the open build.
//
// The open portal list (src/open/portal.ts) is what an open build installs; a
// product build installs it first and then its own. An open page whose
// extension appears only in the product's list is a page the open product
// never shows, so this reads every `: WebExtension` the open pages and app
// define and requires each one, by identity, in the open list. Where the
// checkout holds the open/private manifest, the files it names private are
// left out; where it does not (the open repository), every file is open.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { parseManifest, privateGroupOf } from "@alkera/eslint-config/open-boundary";
import { describe, expect, it } from "vitest";

import { PORTAL_EXTENSIONS } from "@/open/portal";

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const DEFINITION = /export const (\w+): WebExtension\b/g;

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "tests" ? [] : sourceFiles(path);
    return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

function manifestRoot(): string | null {
  let dir = SRC;
  for (;;) {
    if (existsSync(join(dir, "ops", "oss", "boundary.toml"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

function isOpen(file: string): boolean {
  const root = manifestRoot();
  if (root === null) return true;
  const groups = parseManifest(readFileSync(join(root, "ops", "oss", "boundary.toml"), "utf8"));
  return privateGroupOf(groups, relative(root, file).split(sep).join("/")) === null;
}

const definitions = ["pages", "app"]
  .flatMap((dir) => sourceFiles(join(SRC, dir)))
  .filter(isOpen)
  .flatMap((file) =>
    [...readFileSync(file, "utf8").matchAll(DEFINITION)].map((match) => ({ file, name: match[1] })),
  );

describe("the open portal list", () => {
  it("finds the open portal extensions it holds the list to", () => {
    expect(definitions.length).toBeGreaterThan(0);
  });

  it.each(definitions.map(({ file, name }) => [relative(SRC, file), name] as const))(
    "%s: %s is installed by the open build",
    async (file, name) => {
      const module = (await import(/* @vite-ignore */ join(SRC, file))) as Record<string, unknown>;
      expect(PORTAL_EXTENSIONS).toContain(module[name]);
    },
  );
});

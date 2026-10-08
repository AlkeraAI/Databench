import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

// Every `--chat-*` a rule spends must exist.
//
// A custom property nobody declares is not an error anywhere: the rule quietly
// takes its literal fallback, the surface looks approximately right in the
// theme it was written in, and it stops following the light/dark flip and the
// editor's `body[data-alkera-ide]` remap — the one indirection the package
// exists to keep. That is how the result-action buttons came to keep a fixed
// grey ground through both themes while every rule around them moved.
//
// So this reads the sheets rather than the screen: gather the names spent, the
// names declared (in a sheet, or set inline from a component's `style`, which
// is how a per-row depth or gutter arrives), and demand the first set sit
// inside the second.

const CHAT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function filesUnder(dir: string, match: (name: string) => boolean): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...filesUnder(path, match));
    else if (match(entry.name)) found.push(path);
  }
  return found;
}

const read = (paths: string[]): string => paths.map((path) => readFileSync(path, "utf8")).join("\n");

const STYLESHEETS = filesUnder(CHAT, (name) => name.endsWith(".css"));
const COMPONENTS = filesUnder(CHAT, (name) => name.endsWith(".tsx") || name.endsWith(".ts"));

const NAME = /--chat-[a-zA-Z0-9_-]+/g;

/** Names a rule spends: `var(--chat-…)`. */
function spent(css: string): Set<string> {
  const names = new Set<string>();
  for (const call of css.match(/var\(\s*--chat-[a-zA-Z0-9_-]+/g) ?? []) {
    const name = call.match(NAME)?.[0];
    if (name) names.add(name);
  }
  return names;
}

/** Names something declares: a sheet's `--chat-…:` or a component's inline
 *  `"--chat-…":` custom property. */
function declared(source: string): Set<string> {
  const names = new Set<string>();
  for (const declaration of source.match(/--chat-[a-zA-Z0-9_-]+\s*"?\s*:/g) ?? []) {
    const name = declaration.match(NAME)?.[0];
    if (name) names.add(name);
  }
  return names;
}

describe("the chat package's token vocabulary", () => {
  it("declares every name its own rules spend", () => {
    expect(STYLESHEETS.length).toBeGreaterThan(10);
    const known = new Set([...declared(read(STYLESHEETS)), ...declared(read(COMPONENTS))]);
    const undeclared = [...spent(read(STYLESHEETS))].filter((name) => !known.has(name)).sort();
    expect(undeclared).toEqual([]);
  });

  it("finds the token sheet itself, so a rename cannot make this pass by reading nothing", () => {
    const tokens = declared(readFileSync(join(CHAT, "theme", "tokens.css"), "utf8"));
    expect(tokens.has("--chat-ink")).toBe(true);
    expect(tokens.has("--chat-radius-micro")).toBe(true);
  });
});

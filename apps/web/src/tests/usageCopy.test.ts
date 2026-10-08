import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// No surface splits a plan into "covered" and "beyond" any more: on a hosted
// self-serve plan what the plan includes is a share, not a dollar figure, and
// on Enterprise / self-hosted everything is money with its own names. The two
// old labels are the phrases that framed plan usage as dollars, so their
// absence is pinned across the portal and the shared UI, not one page.

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOTS = [resolve(HERE, ".."), resolve(HERE, "../../../../packages/ui/src")];
const SELF = resolve(fileURLToPath(import.meta.url));
const FORBIDDEN = /covered by plan|used beyond plan/i;
const SOURCE = /\.(tsx?|css|md)$/;

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === "node_modules" || entry === "generated" || entry === "dist") continue;
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) walk(path, out);
    else if (SOURCE.test(entry) && path !== SELF) out.push(path);
  }
  return out;
}

describe("usage copy", () => {
  it("names plan usage as neither covered nor beyond anywhere in the portal or the shared UI", () => {
    const offenders: string[] = [];
    for (const root of ROOTS) {
      for (const file of walk(root)) {
        const lines = readFileSync(file, "utf8").split("\n");
        lines.forEach((line, i) => {
          if (FORBIDDEN.test(line)) offenders.push(`${relative(root, file)}:${i + 1}: ${line.trim()}`);
        });
      }
    }
    expect(offenders).toEqual([]);
  });
});

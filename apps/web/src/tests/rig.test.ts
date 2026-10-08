import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// A test that builds its own QueryClient must build it through
// `createQueryClient(...)`, so the rig measures the client the product ships.
// `QueryClient.setDefaultOptions` REPLACES `defaultOptions` rather than merging
// into it, so the two-step `createQueryClient()` +
// `setDefaultOptions({ queries: { retry: false } })` silently drops the
// factory's `staleTime: 5_000` and `refetchOnWindowFocus: false`. Every query in
// such a rig is stale the instant it answers, which makes an exact
// request-count assertion depend on nothing landing after the first answer — a
// late-mounting observer, a remount, a focus change. The factory takes the
// override as an argument (`createQueryClient({ retry: false })`), which spreads
// after the defaults and keeps them.

const testsRoot = resolve(dirname(fileURLToPath(import.meta.url)));
const SELF = relative(testsRoot, fileURLToPath(import.meta.url));

function testFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return testFiles(full);
    return name.endsWith(".ts") || name.endsWith(".tsx") ? [full] : [];
  });
}

const modules = testFiles(testsRoot).filter((f) => relative(testsRoot, f) !== SELF);

describe("no test rig replaces the portal's query defaults", () => {
  it("scans the test tree", () => {
    expect(modules.length).toBeGreaterThan(100);
    expect(modules.some((f) => f.endsWith(".tsx"))).toBe(true);
    expect(modules.some((f) => relative(testsRoot, f) === join("api", "keys.test.ts"))).toBe(true);
  });

  it.each(modules.map((f) => [relative(testsRoot, f), f] as const))("%s", (rel, file) => {
    const offending = readFileSync(file, "utf8")
      .split("\n")
      .flatMap((line, i) => (line.includes("setDefaultOptions(") ? [`${rel}:${i + 1}`] : []));
    expect(offending).toEqual([]);
  });
});

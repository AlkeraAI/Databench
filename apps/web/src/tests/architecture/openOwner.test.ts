// What opening a Files row does is decided in one place, `openTargetOf`
// (lib/files/openTarget.ts). A surface that reads a row's page address itself
// (`objectRoute`) and branches on it opens that row its own way, which is how a
// workspace row once fell through to the object page from one gesture and not
// another. Pages ask the owner; the address is read only under lib/files, plus
// the allowlisted reads below that are links, not opens. The list may only shrink.

import { describe, expect, it } from "vitest";

import { countPerFile, EXTRA_ALLOWLIST, productSources } from "./scan";

const SCANNED_DIRS = [
  "apps/web/src/pages",
  "apps/web/src/app",
  ...(EXTRA_ALLOWLIST.openOwnerDirs ?? []),
];

/** Reads of a row's page that are not an open: each is a link a person follows. */
const ALLOWED: Readonly<Record<string, number>> = {
  // The template's Edit link, shown beside its brief in the details pane.
  "apps/web/src/pages/workspace/files/RightPane.tsx": 1,
};

/** Calls of `objectRoute(` in code (comment lines skipped). */
function pageReads(source: string): number {
  return source
    .split("\n")
    .filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line))
    .reduce((n, line) => n + (line.match(/\bobjectRoute\(/g)?.length ?? 0), 0);
}

describe("page read scanner", () => {
  it.each([
    ["a call", "const route = objectRoute(item);", 1],
    ["a call in a condition", "if (objectRoute(item) !== null) return;", 1],
    ["the owner", "const target = openTargetOf(item);", 0],
    ["a comment", " * objectRoute(item) names the page", 0],
    ["an import", 'import { objectRoute } from "@/lib/files/objectRoute";', 0],
  ])("counts %s", (_why, source, expected) => {
    expect(pageReads(source)).toBe(expected);
  });
});

describe("opening a row outside lib/files", () => {
  const found = countPerFile(SCANNED_DIRS.flatMap(productSources), pageReads);

  it("goes through openTargetOf everywhere but the allowlisted links", () => {
    expect(found).toEqual(ALLOWED);
  });
});

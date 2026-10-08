// Containment is decided on a path segment boundary, never as a bare string
// prefix, and a trailing slash never changes the answer.

import { describe, expect, it } from "vitest";

import { isPathBelow, isPathWithin } from "@/lib/paths";

describe("isPathWithin", () => {
  it.each([
    ["a direct child", "/a/b", "/a/b/c", true],
    ["a deep descendant", "/a/b", "/a/b/c/d/e.txt", true],
    ["the root itself", "/a/b", "/a/b", true],
    ["a sibling that shares the root's characters", "/a/Q3", "/a/Q3-archive", false],
    ["a sibling file under the shared prefix", "/a/Q3", "/a/Q3-archive/x.txt", false],
    ["the parent of the root", "/a/b", "/a", false],
    ["an unrelated tree", "/a/b", "/c/b", false],
    ["a trailing slash on the root", "/a/b/", "/a/b/c", true],
    ["a trailing slash on the root, equal path", "/a/b/", "/a/b", true],
    ["a trailing slash on the target, equal path", "/a/b", "/a/b/", true],
    ["a trailing slash on the root, sibling prefix", "/a/b/", "/a/bc", false],
    ["anything under the drive root", "/", "/a/b", true],
    ["the drive root itself", "/", "/", true],
    ["a relative path under a relative root", "a/b", "a/b/c", true],
    ["a relative sibling prefix", "a/b", "a/bc", false],
  ])("%s", (_label, root, target, expected) => {
    expect(isPathWithin(root, target)).toBe(expected);
  });
});

describe("isPathBelow", () => {
  it.each([
    ["a direct child", "/a/b", "/a/b/c", true],
    ["anything under the drive root", "/", "/a", true],
    ["the root itself", "/a/b", "/a/b", false],
    ["the root itself, spelled with a trailing slash", "/a/b", "/a/b/", false],
    ["the drive root itself", "/", "/", false],
    ["a sibling that shares the root's characters", "/a/Q3", "/a/Q3-archive", false],
  ])("%s", (_label, root, target, expected) => {
    expect(isPathBelow(root, target)).toBe(expected);
  });
});

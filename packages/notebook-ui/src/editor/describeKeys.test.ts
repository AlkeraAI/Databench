// A shortcut is read off the menu row: a key pressed twice must read as two
// presses, not as one label drawn twice.

import { describe, expect, it } from "vitest";

import { describeKeys } from "./commands";

describe("describeKeys", () => {
  it.each([
    ["d d", "mac", "D, D"],
    ["0 0", "other", "0, 0"],
    ["Mod-Enter", "mac", "⌘Enter"],
    ["Mod-Enter", "other", "Ctrl+Enter"],
    ["Mod-Shift-ArrowUp", "mac", "⌘⇧↑"],
    ["a", "mac", "A"],
  ] as const)("%s on %s reads %s", (spec, platform, label) => {
    expect(describeKeys(spec, platform)).toBe(label);
  });
});

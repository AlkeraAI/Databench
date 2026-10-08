import { describe, expect, it } from "vitest";

import { formatBytes as uiFormatBytes } from "@alkera/ui";
import { formatBytes } from "@/lib/format/bytes";

// A preview's "Showing 1.0 MB of 3.1 MB" sits under a header that says "3.1 MB":
// both read a size by the Files list's rule, and this pins that the two copies
// of the rule never drift apart.
describe("the preview reads a size the way the file list does", () => {
  it.each([0, 1, 999, 1000, 1024, 2_621_440, 3_145_728, 13_002_342, 96 * 1000 ** 2, 120 * 1024 ** 2, 1000 ** 3, 1.5 * 1000 ** 4])(
    "for %d bytes",
    (bytes) => {
      expect(uiFormatBytes(bytes)).toBe(formatBytes(bytes));
    },
  );
});

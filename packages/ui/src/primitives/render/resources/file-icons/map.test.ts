import { describe, expect, it } from "vitest";

import { iconNameForFile } from "./map";

describe("a file's icon", () => {
  it("is the notebook glyph for an Notebook, matched before its .py ending", () => {
    expect(iconNameForFile("analysis.alknb.py")).toBe("AlkeraNotebook");
    expect(iconNameForFile("folder/ANALYSIS.ALKNB.PY")).toBe("AlkeraNotebook");
  });

  it("stays Python for a plain Python file", () => {
    expect(iconNameForFile("build.py")).toBe("Python");
  });
});

// @vitest-environment node
// esbuild checks its own TextEncoder against Node's, which jsdom replaces.
import { describe, expect, it } from "vitest";

import { buildFrameModule } from "../../vite/frameModules";

describe("buildFrameModule", () => {
  it.each(["../register", "nb-missing", "register", "NB-HTML", "nb-html/../register"])("builds nothing for %s", async (name) => {
    await expect(buildFrameModule(name)).rejects.toThrow(`There is no frame module named ${name}.`);
  });

  it("builds a module into one IIFE that names its sources", async () => {
    const built = await buildFrameModule("nb-svg");
    expect(built.code).toMatch(/^("use strict";)?\(\(\)=>\{/);
    expect(built.code).not.toMatch(/\bimport\s*\(|\brequire\(|\bexport\b/);
    expect(built.inputs.some((input) => input.endsWith("src/frame/modules/nb-svg.ts"))).toBe(true);
  });
});

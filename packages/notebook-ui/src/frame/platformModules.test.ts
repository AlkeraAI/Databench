import { describe, expect, it } from "vitest";

import type { FrameModule } from "./protocol";
import { loadPlatformFrameModule, platformModuleLoader } from "./platformModules";

/** Runs module code the way the bootstrap does: as an inline script, in a page
 *  whose `__alkRegister` records what registers. jsdom runs page scripts in its
 *  own global, so the recorder is defined by a script too and reports through
 *  the document both sides share. */
function inject(code: string): Array<[string, FrameModule, string]> {
  const run = (text: string): void => {
    const script = document.createElement("script");
    script.textContent = text;
    document.head.appendChild(script);
    script.remove();
  };
  const doc = document as Document & { __registered?: Array<[string, FrameModule, string]> };
  doc.__registered = [];
  run("window.__alkRegister = function (name, version, module) { document.__registered.push([name, module, version]); };");
  run(code);
  run("delete window.__alkRegister;");
  return doc.__registered;
}

describe("platform frame modules", () => {
  it.each([
    ["nb-html", ["text/html"]],
    ["nb-svg", ["image/svg+xml"]],
  ] as const)("%s arrives as one self-contained script that registers itself", async (name, mimes) => {
    const code = await loadPlatformFrameModule(name);
    expect(code).not.toMatch(/\bimport\s*\(|\brequire\(/);
    const registered = inject(code);
    expect(registered.map(([n, m, v]) => [n, v, [...m.mimes]])).toEqual([[name, "1", mimes]]);
  });

  it("answers platform names itself and hands every other name on", async () => {
    const asked: string[] = [];
    const load = platformModuleLoader(async (name, version) => {
      asked.push(`${name}@${version}`);
      return `/* ${name} */`;
    });
    expect(await load("nb-svg", "1")).toBe(await loadPlatformFrameModule("nb-svg"));
    const asset = "alkera-asset:sha256:" + "a".repeat(64);
    expect(await load(asset, "asset")).toBe(`/* ${asset} */`);
    expect(asked).toEqual([`${asset}@asset`]);
  });

  it("answers the widget manager with the platform's own bundle, never another source", async () => {
    const asked: string[] = [];
    const load = platformModuleLoader(async (name) => {
      asked.push(name);
      return "/* impostor */";
    });
    const code = await load("@alkera/widgets", "1");
    expect(code).toContain('"alkera-widgets"');
    expect(code).not.toContain("AlkeraWidgets");
    expect(asked).toEqual([]);
  }, 120_000);

  it("never asks another source for a platform name", async () => {
    const asked: string[] = [];
    const load = platformModuleLoader(async (name) => {
      asked.push(name);
      return "/* impostor */";
    });
    expect(await load("nb-html", "999")).not.toBe("/* impostor */");
    expect(asked).toEqual([]);
  });

  it("refuses a name it has no source for", async () => {
    await expect(platformModuleLoader()("bqplot", "1")).rejects.toThrow("No source for the module bqplot.");
  });
});

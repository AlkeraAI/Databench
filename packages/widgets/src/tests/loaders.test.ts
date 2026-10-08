import { describe, expect, it } from "vitest";

import { ESM_VERSION, EsmImportError, rewriteEsm } from "../loaders/esm";
import { whenRegistered } from "../registry";
import { evaluateAmd, instantiateAmd, publicPathFor } from "../loaders/amd";
import { satisfies } from "../patches/semver";

type Exports = Record<string, unknown>;

/** Runs rewritten ESM as a real module (a data: URL import in Node) and
 *  returns what it registered. */
async function runRewritten(source: string): Promise<Exports> {
  const name = `t${++runs}`;
  const registered = whenRegistered(name, 5_000);
  const { code } = rewriteEsm(source, name);
  await import(/* @vite-ignore */ `data:text/javascript;base64,${btoa(unescape(encodeURIComponent(code)))}`);
  const r = await registered;
  expect(r.version).toBe(ESM_VERSION);
  return r.exports;
}
let runs = 0;

describe("anywidget ESM rewrite", () => {
  it.each([
    { id: "default object", src: "export default { render() { return 1; } };", check: (e: Exports) => typeof (e.default as { render: unknown }).render, expected: "function" },
    { id: "default anonymous function", src: "export default function () { return 2; }", check: (e: Exports) => (e.default as () => number)(), expected: 2 },
    { id: "default named function", src: "export default function make() { return 3; }", check: (e: Exports) => (e.default as () => number)(), expected: 3 },
    { id: "default identifier", src: "const w = { n: 4 }; export default w;", check: (e: Exports) => (e.default as { n: number }).n, expected: 4 },
    { id: "named render and initialize", src: "function r() { return 5; } function i() {} export { r as render, i as initialize };", check: (e: Exports) => (e.render as () => number)(), expected: 5 },
    { id: "export const list", src: "export const a = 6, b = 7;", check: (e: Exports) => (e.a as number) + (e.b as number), expected: 13 },
    { id: "default class", src: "export default class { static v = 8; }", check: (e: Exports) => (e.default as { v: number }).v, expected: 8 },
  ])("hands over the exports: $id", async ({ src, check, expected }) => {
    const exportsObject = await runRewritten(src);
    expect(check(exportsObject)).toBe(expected);
  });

  it.each([
    { id: "static import", src: 'import x from "https://esm.sh/d3"; export default x;', specifier: "https://esm.sh/d3" },
    { id: "bare import", src: 'import * as d3 from "d3"; export default d3;', specifier: "d3" },
    { id: "re-export", src: 'export * from "./other.js";', specifier: "./other.js" },
  ])("refuses a module that loads another: $id", ({ src, specifier }) => {
    expect(() => rewriteEsm(src, "t")).toThrow(EsmImportError);
    try {
      rewriteEsm(src, "t");
    } catch (err) {
      expect((err as EsmImportError).specifier).toBe(specifier);
      expect((err as Error).message).toContain("must bundle its dependencies");
    }
  });

  it("leaves a dynamic import to run time, where the CSP decides", async () => {
    const e = await runRewritten('export const later = () => import("https://cdn.example/x.js"); export const v = 1;');
    expect(e.v).toBe(1);
    expect(typeof e.later).toBe("function");
  });

  it("allows import.meta, which loads nothing", async () => {
    const e = await runRewritten("export const here = typeof import.meta;");
    expect(e.here).toBe("object");
  });
});

describe("AMD shim", () => {
  const run = (code: string) => new Function(code)();

  it("captures deps and factory, and gives the script a publicPath stand-in", () => {
    let seenSrc = "";
    const def = evaluateAmd('define(["a", "exports"], function (a, exports) { exports.v = a.x + 1; });', "lib", (code, publicPath) => {
      seenSrc = publicPath;
      run(code);
    });
    expect(seenSrc).toBe(publicPathFor("lib"));
    expect(seenSrc.startsWith("https://widget-assets.invalid/")).toBe(true);
    expect(instantiateAmd(def, new Map([["a", { x: 1 }]]))).toEqual({ v: 2 });
  });

  it("prefers the definition named for the module", () => {
    const def = evaluateAmd('define("other", [], function () { return { w: 0 }; }); define("lib", [], function () { return { w: 1 }; });', "lib", run);
    expect(instantiateAmd(def, new Map())).toEqual({ w: 1 });
  });

  it("restores the previous define afterwards", () => {
    const g = globalThis as unknown as Record<string, unknown>;
    const before = g.define;
    evaluateAmd("define([], function () { return {}; });", "lib", run);
    expect(g.define).toBe(before);
  });

  it("fails a bundle that defines nothing", () => {
    expect(() => evaluateAmd("var x = 1;", "lib", run)).toThrow("lib did not define a widget module");
  });

  it("gives require only the resolved dependencies", () => {
    const def = evaluateAmd('define(["require", "a"], function (require) { return { a: require("a"), b: (function(){ try { require("b"); } catch (e) { return e.message; } })() }; });', "lib", run);
    expect(instantiateAmd(def, new Map([["a", 1]]))).toEqual({ a: 1, b: "b is not available to this widget" });
  });
});

describe("semver ranges", () => {
  it.each([
    ["1.4.0", ">=1.0.0 <2.0.0", true],
    ["2.0.0", ">=1.0.0 <2.0.0", false],
    ["0.9.9", ">=1.0.0 <2.0.0", false],
    ["1.5.2", "^1.4.0", true],
    ["2.0.0", "^1.4.0", false],
    ["0.13.5", "^0.13.1", true],
    ["0.14.0", "^0.13.1", false],
    ["0.0.3", "^0.0.3", true],
    ["0.0.4", "^0.0.3", false],
    ["1.4.9", "~1.4.0", true],
    ["1.5.0", "~1.4.0", false],
    ["3.0.0", "*", true],
    ["1.0.0", "1.0.0", true],
    ["1.0.1", "=1.0.0", false],
    ["2.1.0", "^1.0.0 || ^2.0.0", true],
    ["not-a-version", "*", false],
  ])("%s in %s is %s", (version, range, expected) => {
    expect(satisfies(version, range)).toBe(expected);
  });
});

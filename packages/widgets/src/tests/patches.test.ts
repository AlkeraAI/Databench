import { describe, expect, it } from "vitest";

import { PatchRegistry } from "../patches/registry";
import { compileExpression } from "../patches/vegaInterpreter";
import sanitize from "../sanitize";

const cell = (value: unknown, row = 0) => ({
  value,
  row,
  metadata: { data: (r: number, col: string) => (({ "0:a": 10, "0:b": 20 }) as Record<string, number>)[`${r}:${col}`] },
});

describe("Vega expression interpreter", () => {
  it.each([
    { expr: "cell.value > 2 ? 'red' : 'blue'", value: 3, out: "red" },
    { expr: "cell.value > 2 ? 'red' : 'blue'", value: 1, out: "blue" },
    { expr: "if(cell.value % 2 == 0, 'even', 'odd')", value: 4, out: "even" },
    { expr: "max(cell.value, 10) * 2", value: 3, out: 20 },
    { expr: "cell.metadata.data['a'] + cell.metadata.data['b']", value: 0, out: 30 },
    { expr: "[cell.value, -cell.value][1]", value: 5, out: -5 },
    { expr: "{k: cell.value}.k", value: 6, out: 6 },
    { expr: "!(cell.value === 1) && PI > 3", value: 2, out: true },
    { expr: "'abc'.toUpperCase()", value: 0, out: "ABC" },
    { expr: "default_value", value: 0, out: "dv" },
  ])("$expr with $value gives $out", ({ expr, value, out }) => {
    expect(compileExpression(expr)({ vars: { cell: cell(value), default_value: "dv" }, functions: {} })).toBe(out);
  });

  it.each([
    "cell.constructor",
    "cell['__proto__']",
    "cell.value.constructor.constructor('return 1')",
    "cell['constr' + 'uctor']",
    "{__proto__: 1}",
    "'x'.constructor",
    "cell.metadata.prototype",
  ])("refuses %s", (expr) => {
    expect(() => compileExpression(expr)({ vars: { cell: cell(1) }, functions: {} })).toThrow(/may not use/);
  });

  it.each(["window", "globalThis", "document", "Function"])("does not reach the global %s", (name) => {
    expect(() => compileExpression(name)({ vars: { cell: cell(1) } })).toThrow(`unknown identifier ${name}`);
  });

  it("calls functions from the grid's table with that table as this", () => {
    const functions = { scaled(this: { factor: number }, v: number) { return v * this.factor; }, factor: 3 };
    expect(compileExpression("scaled(cell.value)")({ vars: { cell: cell(2) }, functions })).toBe(6);
  });
});

describe("patch registry", () => {
  function registryWith(range: string) {
    const r = new PatchRegistry();
    r.register({ name: "p", module: "lib", range, apply: (e) => ((e.patched = true), true) });
    return r;
  }

  it.each([
    { version: "1.4.0", range: "^1.0.0", applied: ["p"] },
    { version: "2.0.0", range: "^1.0.0", applied: [] },
    { version: "env", range: "^1.0.0", applied: ["p"] },
    { version: "*", range: "^1.0.0", applied: ["p"] },
  ])("version $version against $range", ({ version, range, applied }) => {
    const exports: Record<string, unknown> = {};
    expect(registryWith(range).apply("lib", version, exports)).toEqual(applied);
    expect(exports.patched === true).toBe(applied.length > 0);
  });

  it("never applies to another module", () => {
    expect(registryWith("*").apply("other", "1.0.0", {})).toEqual([]);
  });

  it("reports only patches whose shape matched", () => {
    const r = new PatchRegistry();
    r.register({ name: "no-shape", module: "lib", range: "*", apply: () => false });
    expect(r.apply("lib", "1.0.0", {})).toEqual([]);
  });
});

// The options ManagerBase.inline_sanitize passes.
const OPTIONS = {
  allowedTags: ["a", "abbr", "b", "code", "em", "i", "img", "li", "ol", "span", "strong", "ul"],
  allowedAttributes: { "*": ["aria-*", "class", "style", "title"], a: ["href"], img: ["src"], style: ["media", "type"] },
};

describe("frame sanitizer (sanitize-html stand-in)", () => {
  it.each([
    { id: "kept markup", html: '<b class="x">bold</b> <i>it</i>', out: '<b class="x">bold</b> <i>it</i>' },
    { id: "script dropped with content", html: "a<script>alert(1)</script>b", out: "ab" },
    { id: "style element dropped", html: "a<style>*{}</style>b", out: "ab" },
    { id: "unknown tag unwrapped", html: "<div><b>x</b></div>", out: "<b>x</b>" },
    { id: "event handler removed", html: '<span onclick="evil()" title="t">s</span>', out: '<span title="t">s</span>' },
    { id: "javascript url removed", html: '<a href="javascript:alert(1)">l</a>', out: "<a>l</a>" },
    { id: "obfuscated javascript url removed", html: '<a href="jav&#x09;ascript:alert(1)">l</a>', out: "<a>l</a>" },
    { id: "data url on img refused by default schemes", html: '<img src="data:image/png;base64,AA">', out: "<img>" },
    { id: "https link kept", html: '<a href="https://e.com">l</a>', out: '<a href="https://e.com">l</a>' },
    { id: "relative link kept", html: '<a href="#x">l</a>', out: '<a href="#x">l</a>' },
    { id: "aria wildcard", html: '<span aria-label="n" data-x="1">s</span>', out: '<span aria-label="n">s</span>' },
    { id: "comment removed", html: "a<!-- c -->b", out: "ab" },
    { id: "iframe unwrapped to nothing", html: '<iframe src="https://e.com"></iframe>t', out: "t" },
  ])("$id", ({ html, out }) => {
    expect(sanitize(html, OPTIONS)).toBe(out);
  });
});

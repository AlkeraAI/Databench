import { compileQuery, expandReplacement, findMatches, outputText, replaceAll, replaceOne } from "./find";
import type { FindOptions } from "./find";
import type { CellOutput, DocCell } from "./types";

function cell(id: string, source: string, kind = "python"): DocCell {
  return { id, kind, name: "_", source, config: {}, meta: {} };
}

const all = { code: true, markdown: true, outputs: true };
const codeOnly = { code: true, markdown: false, outputs: false };

const cells = [
  cell("py", "total = 1\nTotal = total + 2"),
  cell("md", "# Total\nThe total is shown.", "markdown"),
  cell("sql", "select total from t", "sql"),
];
const runtime = {
  py: {
    outputs: [
      { output_id: "o1", type: "stream", name: "stdout", text: "total: 3\n" },
      { output_id: "o2", type: "display", data: { "text/plain": ["sub", "total"], "text/html": "<b>total</b>" } },
      { output_id: "o3", type: "error", error: { ename: "E", evalue: "total", traceback: [] } },
    ] as CellOutput[],
  },
};

function opts(query: string, extra: Partial<FindOptions> = {}): FindOptions {
  return { query, scope: all, ...extra };
}

describe("findMatches", () => {
  it("finds across code, Markdown and outputs, in document order", () => {
    const result = findMatches(cells, runtime, opts("total"));
    expect(result).toEqual({
      ok: true,
      matches: [
        { cell_id: "py", where: "source", from: 0, to: 5, line: 1 },
        { cell_id: "py", where: "source", from: 10, to: 15, line: 2 },
        { cell_id: "py", where: "source", from: 18, to: 23, line: 2 },
        { cell_id: "py", where: "output", output_id: "o1", from: 0, to: 5, line: 1 },
        { cell_id: "py", where: "output", output_id: "o2", from: 3, to: 8, line: 1 },
        { cell_id: "md", where: "source", from: 2, to: 7, line: 1 },
        { cell_id: "md", where: "source", from: 12, to: 17, line: 2 },
        { cell_id: "sql", where: "source", from: 7, to: 12, line: 1 },
      ],
    });
  });

  it.each([
    ["code only", { code: true, markdown: false, outputs: false }, ["py", "py", "py", "sql"]],
    ["Markdown only", { code: false, markdown: true, outputs: false }, ["md", "md"]],
    ["outputs only", { code: false, markdown: false, outputs: true }, ["py", "py"]],
    ["nothing", { code: false, markdown: false, outputs: false }, []],
  ])("limits the scope to %s", (_label, scope, ids) => {
    const result = findMatches(cells, runtime, opts("total", { scope }));
    expect(result.ok && result.matches.map((m) => m.cell_id)).toEqual(ids);
  });

  it("matches case only when asked", () => {
    const sensitive = findMatches([cells[0]], {}, opts("Total", { caseSensitive: true }));
    expect(sensitive.ok && sensitive.matches.map((m) => m.from)).toEqual([10]);
    const insensitive = findMatches([cells[0]], {}, opts("Total"));
    expect(insensitive.ok && insensitive.matches).toHaveLength(3);
  });

  it.each([
    ["a whole word", "total", "total totals subtotal total_x total.", [0, 30]],
    ["a word next to Unicode letters", "df", "édf df dfé", [4]],
    ["a word next to digits", "x", "x1 x 2x", [3]],
    ["a regex word", "t\\w+", "tab stab tic", [0, 9]],
  ])("finds %s with whole word on", (_label, query, source, starts) => {
    const regex = query.includes("\\");
    const result = findMatches([cell("c", source)], {}, opts(query, { wholeWord: true, regex }));
    expect(result.ok && result.matches.map((m) => m.from)).toEqual(starts);
  });

  it("treats regex characters literally unless regex is on", () => {
    const source = "a.b axb (x) a-b";
    const literal = findMatches([cell("c", source)], {}, opts("a.b"));
    expect(literal.ok && literal.matches.map((m) => m.from)).toEqual([0]);
    const pattern = findMatches([cell("c", source)], {}, opts("a.b", { regex: true }));
    expect(pattern.ok && pattern.matches.map((m) => m.from)).toEqual([0, 4, 12]);
    const parens = findMatches([cell("c", source)], {}, opts("(x)"));
    expect(parens.ok && parens.matches.map((m) => m.from)).toEqual([8]);
    const dash = findMatches([cell("c", source)], {}, opts("a-b"));
    expect(dash.ok && dash.matches.map((m) => m.from)).toEqual([12]);
  });

  it.each(["(", "[a-", "a{2,1}", "(?<n>x)(?<n>y)"])("returns a typed error for the invalid regex %j", (query) => {
    const result = findMatches(cells, runtime, opts(query, { regex: true }));
    expect(result.ok).toBe(false);
    expect(!result.ok && result.error.kind).toBe("invalid_regex");
    expect(!result.ok && result.error.message).not.toBe("");
  });

  it("finds nothing for an empty query", () => {
    expect(findMatches(cells, runtime, opts(""))).toEqual({ ok: true, matches: [] });
  });

  it("skips empty regex matches without looping", () => {
    const result = findMatches([cell("c", "b𝔘aab")], {}, opts("a*", { regex: true }));
    expect(result.ok && result.matches.map((m) => [m.from, m.to])).toEqual([[3, 5]]);
  });

  it("counts offsets in UTF-16 units past astral characters", () => {
    const result = findMatches([cell("c", "𝔘𝔘 x\n𝔘x")], {}, opts("x"));
    expect(result.ok && result.matches.map((m) => [m.from, m.line])).toEqual([
      [5, 1],
      [9, 2],
    ]);
  });

  it("matches an astral character as one character in regex mode", () => {
    const result = findMatches([cell("c", "a𝔘b")], {}, opts("a.b", { regex: true }));
    expect(result.ok && result.matches.map((m) => [m.from, m.to])).toEqual([[0, 4]]);
  });
});

describe("outputText", () => {
  it.each([
    ["a stream", { output_id: "s", type: "stream", name: "stderr", text: "warn" }, "warn"],
    ["plain text", { output_id: "d", type: "display", data: { "text/plain": "x" } }, "x"],
    ["plain text in parts", { output_id: "d", type: "display", data: { "text/plain": ["a", "b"] } }, "ab"],
    ["only HTML", { output_id: "d", type: "display", data: { "text/html": "<p>x</p>" } }, null],
    ["a non-text plain value", { output_id: "d", type: "display", data: { "text/plain": 3 } }, null],
    ["an error", { output_id: "e", type: "error", error: { ename: "E", evalue: "v", traceback: [] } }, null],
  ])("reads %s", (_label, output, expected) => {
    expect(outputText(output as CellOutput)).toBe(expected);
  });
});

describe("replaceAll", () => {
  it("writes one replace op per changed cell and never an output", () => {
    const result = replaceAll(cells, opts("total", { caseSensitive: true }), "sum");
    expect(result).toEqual({
      ok: true,
      count: 4,
      ops: [
        { op: "replace", cell_id: "py", source: "sum = 1\nTotal = sum + 2" },
        { op: "replace", cell_id: "md", source: "# Total\nThe sum is shown." },
        { op: "replace", cell_id: "sql", source: "select sum from t" },
      ],
    });
  });

  it("writes nothing when only outputs are in scope", () => {
    expect(replaceAll(cells, opts("total", { scope: { code: false, markdown: false, outputs: true } }), "sum")).toEqual({
      ok: true,
      ops: [],
      count: 0,
    });
  });

  it("substitutes groups in regex mode", () => {
    const source = "load_a(1); load_b(2)";
    const result = replaceAll([cell("c", source)], opts("load_(\\w)\\((\\d)\\)", { regex: true, scope: codeOnly }), "read($2, '$1') $$ $&");
    expect(result.ok && result.ops[0].source).toBe("read(1, 'a') $ load_a(1); read(2, 'b') $ load_b(2)");
  });

  it("inserts `$1` literally when regex is off", () => {
    const result = replaceAll([cell("c", "x")], opts("x", { scope: codeOnly }), "$1$&");
    expect(result.ok && result.ops[0].source).toBe("$1$&");
  });

  it("returns the regex error", () => {
    expect(replaceAll(cells, opts("(", { regex: true }), "x")).toMatchObject({ ok: false, error: { kind: "invalid_regex" } });
  });

  it("returns no ops for an empty query", () => {
    expect(replaceAll(cells, opts(""), "x")).toEqual({ ok: true, ops: [], count: 0 });
  });

  it("drops a cell whose text does not change", () => {
    expect(replaceAll([cell("c", "same")], opts("same", { scope: codeOnly }), "same")).toEqual({ ok: true, ops: [], count: 1 });
  });
});

describe("replaceOne", () => {
  it("replaces exactly the match it is given", () => {
    const found = findMatches(cells, runtime, opts("total", { caseSensitive: true }));
    const second = found.ok ? found.matches[1] : undefined;
    expect(second).toMatchObject({ cell_id: "py", from: 18 });
    expect(replaceOne(cells, second!, opts("total", { caseSensitive: true }), "sum")).toEqual({
      ok: true,
      count: 1,
      ops: [{ op: "replace", cell_id: "py", source: "total = 1\nTotal = sum + 2" }],
    });
  });

  it("refuses to replace in an output", () => {
    const match = { cell_id: "py", where: "output" as const, output_id: "o1", from: 0, to: 5, line: 1 };
    expect(replaceOne(cells, match, opts("total"), "sum")).toEqual({ ok: false, error: { kind: "output_read_only" } });
  });

  it.each([
    ["a moved match", { cell_id: "py", where: "source" as const, from: 1, to: 6, line: 1 }, "total"],
    ["a missing cell", { cell_id: "gone", where: "source" as const, from: 0, to: 5, line: 1 }, "total"],
    ["an empty query", { cell_id: "py", where: "source" as const, from: 0, to: 5, line: 1 }, ""],
  ])("refuses %s as stale", (_label, match, query) => {
    expect(replaceOne(cells, match, opts(query), "sum")).toEqual({ ok: false, error: { kind: "stale_match" } });
  });

  it("returns the regex error", () => {
    const match = { cell_id: "py", where: "source" as const, from: 0, to: 5, line: 1 };
    expect(replaceOne(cells, match, opts("(", { regex: true }), "x")).toMatchObject({ ok: false, error: { kind: "invalid_regex" } });
  });

  it("expands groups for a single regex match", () => {
    const match = { cell_id: "c", where: "source" as const, from: 0, to: 3, line: 1 };
    const result = replaceOne([cell("c", "abc abc")], match, opts("(a)(b)c", { regex: true, scope: codeOnly }), "$2$1");
    expect(result.ok && result.ops[0].source).toBe("ba abc");
  });
});

describe("expandReplacement", () => {
  const exec = /(?<first>a)(b)(c)(d)(e)(f)(g)(h)(i)(j)(k)/u.exec("abcdefghijk")!;

  it.each([
    ["$1", "a"],
    ["$11", "k"],
    ["$12", "a2"],
    ["$0", "$0"],
    ["$<first>", "a"],
    ["$<nope>", ""],
    ["$$1", "$1"],
    ["$&!", "abcdefghijk!"],
    ["$x", "$x"],
  ])("expands %j", (template, expected) => {
    expect(expandReplacement(template, exec)).toBe(expected);
  });

  it("keeps a named reference when the pattern has no names", () => {
    expect(expandReplacement("$<n>", /(a)/u.exec("a")!)).toBe("$<n>");
  });

  it("keeps a group number past the last group", () => {
    expect(expandReplacement("$3", /(a)/u.exec("a")!)).toBe("$3");
  });
});

describe("compileQuery", () => {
  it("is case-insensitive by default", () => {
    const re = compileQuery({ query: "x" });
    expect(re instanceof RegExp && re.flags).toContain("i");
  });
});

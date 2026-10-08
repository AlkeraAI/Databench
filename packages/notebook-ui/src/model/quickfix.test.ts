import { checkNewName, makeLocal, renameDefinition } from "./quickfix";
import type { DocCell, ErrorInfo, GraphView } from "./types";

function cell(id: string, source: string, kind = "python"): DocCell {
  return { id, kind, name: "_", source, config: {}, meta: {} };
}

const conflict: ErrorInfo = {
  ename: "MultipleDefinitionError",
  evalue: "df is defined by several cells",
  traceback: [],
  kind: "multiple_definitions",
  names: ["df"],
  cells: ["b"],
};

describe("makeLocal", () => {
  it("renames the name to its cell-local form in this cell only", () => {
    const cells = [cell("a", "df = load()\nprint(df.shape)  # df"), cell("b", "df = other()"), cell("c", "x = df")];
    expect(makeLocal("a", conflict, cells)).toEqual({
      ok: true,
      ops: [{ op: "replace", cell_id: "a", source: "_df = load()\nprint(_df.shape)  # df" }],
    });
  });

  it("renames every conflicting name the cell defines and skips the others", () => {
    const error = { ...conflict, names: ["df", "model", "other"] };
    const cells = [cell("a", "df = 1\nmodel = fit(df)")];
    const result = makeLocal("a", error, cells, { a: { defs: ["df", "model"], refs: ["fit"] } });
    expect(result).toEqual({ ok: true, ops: [{ op: "replace", cell_id: "a", source: "_df = 1\n_model = fit(_df)" }] });
  });

  it("trusts the engine's defs over its own reading", () => {
    const cells = [cell("a", "df = 1")];
    expect(makeLocal("a", conflict, cells, { a: { defs: [], refs: [] } })).toEqual({
      ok: false,
      refusal: { reason: "not_defined_here", name: "df" },
    });
  });

  it("turns a bare import into an aliased one", () => {
    const error = { ...conflict, names: ["np"] };
    expect(makeLocal("a", error, [cell("a", "import np\nnp.zeros(3)")])).toEqual({
      ok: true,
      ops: [{ op: "replace", cell_id: "a", source: "import np as _np\n_np.zeros(3)" }],
    });
  });

  it.each([
    ["another error kind", { ...conflict, kind: "cycle" }, [cell("a", "df = 1")], "a", { reason: "not_multiple_definitions" }],
    ["an exception", { ...conflict, kind: undefined }, [cell("a", "df = 1")], "a", { reason: "not_multiple_definitions" }],
    ["a missing cell", conflict, [cell("a", "df = 1")], "zz", { reason: "cell_not_found", cell_id: "zz" }],
    ["a SQL cell", conflict, [cell("a", "select 1", "sql")], "a", { reason: "not_python", cell_id: "a" }],
    ["a cell that only reads the name", conflict, [cell("a", "print(df)")], "a", { reason: "not_defined_here", name: "df" }],
    ["an error with no names", { ...conflict, names: [] }, [cell("a", "df = 1")], "a", { reason: "not_defined_here", name: "" }],
    ["a local name already in use", conflict, [cell("a", "_df = 0\ndf = _df")], "a", { reason: "name_in_use", name: "_df", cell_id: "a" }],
    ["a dotted import", conflict, [cell("a", "import df.sub")], "a", { reason: "cannot_rename", name: "df", cell_id: "a" }],
  ])("refuses %s", (_label, error, cells, id, refusal) => {
    expect(makeLocal(id, error as ErrorInfo, cells)).toEqual({ ok: false, refusal });
  });

  it("treats a kind this build does not know as Python", () => {
    expect(makeLocal("a", conflict, [cell("a", "df = 1", "future_kind")]).ok).toBe(true);
  });
});

describe("renameDefinition", () => {
  const cells = [
    cell("before", "y = df"),
    cell("a", "df = load()"),
    cell("b", "df = other()"),
    cell("reader", "print(df, obj.df, f'{df}', 'df')"),
    cell("redefiner", "df = 3\nprint(df)"),
    cell("unrelated", "x = 1"),
  ];

  it("renames the definition and every later reader, in document order", () => {
    expect(renameDefinition({ cellId: "a", name: "df", newName: "raw", cells })).toEqual({
      ok: true,
      ops: [
        { op: "replace", cell_id: "a", source: "raw = load()" },
        { op: "replace", cell_id: "reader", source: "print(raw, obj.df, f'{raw}', 'df')" },
      ],
    });
  });

  it("with a graph, updates only downstream readers", () => {
    const graph: GraphView = { edges: [["b", "reader"]], errors: {} };
    expect(renameDefinition({ cellId: "a", name: "df", newName: "raw", cells, graph })).toEqual({
      ok: true,
      ops: [{ op: "replace", cell_id: "a", source: "raw = load()" }],
    });
    const fromA: GraphView = { edges: [["a", "reader"]], errors: {} };
    const result = renameDefinition({ cellId: "a", name: "df", newName: "raw", cells, graph: fromA });
    expect(result.ok && result.ops.map((op) => op.cell_id)).toEqual(["a", "reader"]);
  });

  it("uses the engine's refs to pick readers", () => {
    const runtime = {
      a: { defs: ["df"], refs: [] },
      reader: { defs: [], refs: [] },
      unrelated: { defs: ["x"], refs: ["df"] },
    };
    const result = renameDefinition({ cellId: "a", name: "df", newName: "raw", cells, runtime });
    // `unrelated` reports a ref but never mentions it as a variable, so its text is unchanged.
    expect(result.ok && result.ops.map((op) => op.cell_id)).toEqual(["a"]);
  });

  it.each([
    ["the same name", "df", { reason: "same_name", name: "df" }],
    ["a keyword", "class", { reason: "keyword", name: "class" }],
    ["a non-identifier", "my df", { reason: "invalid_name", name: "my df" }],
    ["an empty name", "", { reason: "invalid_name", name: "" }],
    ["a name another cell defines", "x", { reason: "already_defined", name: "x", cell_id: "unrelated" }],
    ["a name this cell already uses", "load", { reason: "name_in_use", name: "load", cell_id: "a" }],
  ])("refuses %s", (_label, newName, refusal) => {
    expect(renameDefinition({ cellId: "a", name: "df", newName, cells })).toEqual({ ok: false, refusal });
  });

  it("refuses a name a reader already uses", () => {
    const local = [cell("a", "df = 1"), cell("r", "print(df, raw)")];
    expect(renameDefinition({ cellId: "a", name: "df", newName: "raw", cells: local })).toEqual({
      ok: false,
      refusal: { reason: "name_in_use", name: "raw", cell_id: "r" },
    });
  });

  it("refuses when a reader is not Python", () => {
    const local = [cell("a", "df = 1"), cell("q", "select * from {df}", "sql")];
    const runtime = { a: { defs: ["df"], refs: [] }, q: { defs: [], refs: ["df"] } };
    expect(renameDefinition({ cellId: "a", name: "df", newName: "raw", cells: local, runtime })).toEqual({
      ok: false,
      refusal: { reason: "reader_not_python", name: "df", cell_id: "q" },
    });
  });

  it.each([
    ["a missing cell", "zz", { reason: "cell_not_found", cell_id: "zz" }],
    ["a cell that does not define the name", "reader", { reason: "not_defined_here", name: "df" }],
  ])("refuses %s", (_label, cellId, refusal) => {
    expect(renameDefinition({ cellId, name: "df", newName: "raw", cells })).toEqual({ ok: false, refusal });
  });

  it("refuses a Markdown cell", () => {
    const local = [cell("m", "# df", "markdown")];
    expect(renameDefinition({ cellId: "m", name: "df", newName: "raw", cells: local, runtime: { m: { defs: ["df"], refs: [] } } })).toEqual({
      ok: false,
      refusal: { reason: "not_python", cell_id: "m" },
    });
  });

  it("refuses a definition the rename cannot reach", () => {
    expect(renameDefinition({ cellId: "a", name: "df", newName: "raw", cells: [cell("a", "import df.sub")] })).toEqual({
      ok: false,
      refusal: { reason: "cannot_rename", name: "df", cell_id: "a" },
    });
  });
});

describe("checkNewName", () => {
  it("accepts a fresh identifier", () => {
    expect(checkNewName("df", "raw_df")).toBeNull();
  });
});

import { cellLinks, childrenOf, deriveGraph, downstreamOf, parentsOf, topologicalOrder, upstreamOf } from "./graph";
import type { GraphView } from "./types";

// load -> clean -> train -> report, and load -> report; plot reads clean.
const chain: GraphView = {
  edges: [
    ["load", "clean"],
    ["clean", "train"],
    ["train", "report"],
    ["load", "report"],
    ["clean", "plot"],
  ],
  errors: {},
};
const order = ["report", "plot", "train", "clean", "load"];

describe("direct neighbours", () => {
  it.each([
    ["report", ["train", "load"], []],
    ["clean", ["load"], ["plot", "train"]],
    ["load", [], ["report", "clean"]],
    ["missing", [], []],
  ])("for %s, in document order", (id, parents, children) => {
    expect(parentsOf(chain, id, order)).toEqual(parents);
    expect(childrenOf(chain, id, order)).toEqual(children);
  });

  it("ignores self edges", () => {
    const view: GraphView = { edges: [["a", "a"], ["a", "b"]], errors: {} };
    expect(parentsOf(view, "a")).toEqual([]);
    expect(childrenOf(view, "a")).toEqual(["b"]);
  });

  it("orders a cell the document does not name by its first edge", () => {
    expect(childrenOf(chain, "load")).toEqual(["clean", "report"]);
  });
});

describe("closures", () => {
  it.each([
    ["report", ["train", "clean", "load"], []],
    ["train", ["clean", "load"], ["report"]],
    ["load", [], ["report", "plot", "train", "clean"]],
    ["plot", ["clean", "load"], []],
  ])("upstream and downstream of %s", (id, up, down) => {
    expect(upstreamOf(chain, id, order)).toEqual(up);
    expect(downstreamOf(chain, id, order)).toEqual(down);
  });

  it("stops on a cycle and never includes the cell itself", () => {
    const cycle: GraphView = { edges: [["a", "b"], ["b", "c"], ["c", "a"], ["c", "d"]], errors: {} };
    expect(upstreamOf(cycle, "a", ["a", "b", "c", "d"])).toEqual(["b", "c"]);
    expect(downstreamOf(cycle, "a", ["a", "b", "c", "d"])).toEqual(["b", "c", "d"]);
  });
});

describe("topologicalOrder", () => {
  it("puts parents first and breaks ties by document order", () => {
    expect(topologicalOrder(chain, order)).toEqual({ order: ["load", "clean", "plot", "train", "report"], cyclic: [] });
  });

  it("keeps document order for cells with no edges", () => {
    expect(topologicalOrder({ edges: [], errors: {} }, ["c", "a", "b"]).order).toEqual(["c", "a", "b"]);
  });

  it("prefers the earlier ready cell even when it became ready later", () => {
    // `z` is free from the start; `a` (earlier in the document) only after `y`.
    const view: GraphView = { edges: [["y", "a"]], errors: {} };
    expect(topologicalOrder(view, ["a", "y", "z"]).order).toEqual(["y", "a", "z"]);
  });

  it("puts cycles last, in document order, and names them", () => {
    const view: GraphView = { edges: [["b", "c"], ["c", "b"], ["c", "d"], ["a", "e"]], errors: {} };
    expect(topologicalOrder(view, ["d", "c", "b", "a", "e"])).toEqual({
      order: ["a", "e", "d", "c", "b"],
      cyclic: ["d", "c", "b"],
    });
  });

  it("includes cells only the edges name", () => {
    expect(topologicalOrder({ edges: [["x", "y"]], errors: {} }, []).order).toEqual(["x", "y"]);
  });
});

describe("cellLinks", () => {
  const names = {
    load: { defs: ["raw", "meta"], refs: [] },
    clean: { defs: ["df"], refs: ["raw", "pd"] },
    train: { defs: ["model"], refs: ["df"] },
    report: { defs: [], refs: ["model", "meta", "raw"] },
  };

  it("names what flows along each edge", () => {
    expect(cellLinks(chain, "report", names, order)).toEqual({
      readsFrom: [
        { cell_id: "train", names: ["model"] },
        { cell_id: "load", names: ["raw", "meta"] },
      ],
      readBy: [],
    });
    expect(cellLinks(chain, "load", names, order).readBy).toEqual([
      { cell_id: "report", names: ["raw", "meta"] },
      { cell_id: "clean", names: ["raw"] },
    ]);
  });

  it("keeps an edge whose names are unknown", () => {
    expect(cellLinks(chain, "plot", names, order).readsFrom).toEqual([{ cell_id: "clean", names: [] }]);
  });
});

describe("deriveGraph", () => {
  it("links each ref to every cell that defines it, without self edges or repeats", () => {
    const cells = [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }];
    const view = deriveGraph(cells, {
      a: { defs: ["x", "y"], refs: [] },
      b: { defs: ["x"], refs: ["x"] },
      c: { defs: [], refs: ["x", "y", "nothing"] },
      d: undefined,
    });
    expect(view.edges).toEqual([
      ["a", "b"],
      ["a", "c"],
      ["b", "c"],
    ]);
  });
});

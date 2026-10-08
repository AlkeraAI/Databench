// How editor groups are arranged: the pure tree every split, close and drop
// goes through, and the stored form a document carries it in.

import { describe, expect, it } from "vitest";

import {
  MAX_GROUPS,
  MIN_SHARE,
  fromWire,
  groupOrder,
  leaf,
  neighbour,
  normalShares,
  nudgeShares,
  reconcile,
  removeGroup,
  splitAt,
  toWire,
  type LayoutNode,
} from "@/pages/workspace/chat/workspace/editorLayout";

const row = (...ids: string[]): LayoutNode => ({
  kind: "split",
  axis: "row",
  children: ids.map(leaf),
  sizes: ids.map(() => 1 / ids.length),
});

describe("splitting a group", () => {
  it("puts the new group beside the old one, each with half its space", () => {
    expect(splitAt(leaf("a"), "a", "b", "right")).toEqual({
      kind: "split",
      axis: "row",
      children: [leaf("a"), leaf("b")],
      sizes: [0.5, 0.5],
    });
  });

  it.each([
    ["left", "row", ["b", "a"]],
    ["right", "row", ["a", "b"]],
    ["up", "column", ["b", "a"]],
    ["down", "column", ["a", "b"]],
  ] as const)("%s lays them out along the %s, in that order", (side, axis, order) => {
    const split = splitAt(leaf("a"), "a", "b", side);
    expect(split.kind === "split" && split.axis).toBe(axis);
    expect(groupOrder(split)).toEqual(order);
  });

  it("joins a row it is already in, halving only the group it came from", () => {
    const split = splitAt({ kind: "split", axis: "row", children: [leaf("a"), leaf("b")], sizes: [0.6, 0.4] }, "a", "c", "right");
    expect(groupOrder(split)).toEqual(["a", "c", "b"]);
    expect(split.kind === "split" && split.sizes.map((size) => Number(size.toFixed(3)))).toEqual([0.3, 0.3, 0.4]);
  });

  it("splits across a row by nesting a column where the group stood", () => {
    const split = splitAt(row("a", "b"), "b", "c", "down");
    expect(split).toEqual({
      kind: "split",
      axis: "row",
      children: [leaf("a"), { kind: "split", axis: "column", children: [leaf("b"), leaf("c")], sizes: [0.5, 0.5] }],
      sizes: [0.5, 0.5],
    });
  });

  it("leaves the layout alone for a group it does not hold", () => {
    expect(splitAt(row("a", "b"), "zz", "c", "right")).toEqual(row("a", "b"));
  });
});

describe("closing a group", () => {
  it("gives its space to the neighbours it shared a split with", () => {
    const after = removeGroup({ kind: "split", axis: "row", children: [leaf("a"), leaf("b"), leaf("c")], sizes: [0.5, 0.25, 0.25] }, "b");
    expect(groupOrder(after as LayoutNode)).toEqual(["a", "c"]);
    expect(after?.kind === "split" && after.sizes.map((size) => Number(size.toFixed(3)))).toEqual([0.667, 0.333]);
  });

  it("collapses a split left with one child into that child, and folds a row into its parent row", () => {
    const nested: LayoutNode = {
      kind: "split",
      axis: "row",
      children: [leaf("a"), { kind: "split", axis: "column", children: [leaf("b"), row("c", "d")], sizes: [0.5, 0.5] }],
      sizes: [0.5, 0.5],
    };
    const after = removeGroup(nested, "b") as LayoutNode;
    // The column is gone (one child), and the row it held joins the outer row.
    expect(after.kind === "split" && after.axis).toBe("row");
    expect(after.kind === "split" && after.children.every((child) => child.kind === "group")).toBe(true);
    expect(groupOrder(after)).toEqual(["a", "c", "d"]);
  });

  it("answers null when it was the only group", () => {
    expect(removeGroup(leaf("a"), "a")).toBeNull();
  });
});

describe("the group beside another", () => {
  const grid: LayoutNode = {
    kind: "split",
    axis: "row",
    children: [leaf("a"), { kind: "split", axis: "column", children: [leaf("b"), leaf("c")], sizes: [0.5, 0.5] }],
    sizes: [0.5, 0.5],
  };

  it.each([
    ["a", "right", "b"],
    ["b", "left", "a"],
    ["c", "left", "a"],
    ["b", "down", "c"],
    ["c", "up", "b"],
    ["a", "left", null],
    ["b", "right", null],
    ["a", "down", null],
  ] as const)("%s, %s: %s", (group, side, expected) => {
    expect(neighbour(grid, group, side)).toBe(expected);
  });
});

describe("shares", () => {
  it("are equal when the stored list does not add up to one per child", () => {
    expect(normalShares([0.5], 2)).toEqual([0.5, 0.5]);
    expect(normalShares([0.5, "wide"], 2)).toEqual([0.5, 0.5]);
    expect(normalShares([0.5, -1], 2)).toEqual([0.5, 0.5]);
  });

  it("are lifted to the floor and renormalized", () => {
    const shares = normalShares([0.99, 0.01], 2);
    expect(shares[1]).toBeGreaterThanOrEqual(MIN_SHARE - 1e-9);
    expect((shares[0] ?? 0) + (shares[1] ?? 0)).toBeCloseTo(1);
  });

  it("move only the two either side of a nudged boundary, and stop at the floor", () => {
    expect(nudgeShares([0.25, 0.25, 0.5], 0, 0.1).map((size) => Number(size.toFixed(2)))).toEqual([0.35, 0.15, 0.5]);
    const pinned = nudgeShares([0.5, 0.5], 0, 0.9);
    expect(pinned[1]).toBeCloseTo(MIN_SHARE);
    expect(pinned[0]).toBeCloseTo(1 - MIN_SHARE);
  });
});

describe("reconciling the layout with the groups the tabs name", () => {
  it("drops a group nobody is in and adds a missing one at the end of the row", () => {
    expect(groupOrder(reconcile(row("a", "b"), ["a", "c"]))).toEqual(["a", "c"]);
  });

  it("starts from one group when there is no layout", () => {
    expect(reconcile(null, ["x"])).toEqual(leaf("x"));
  });

  it("never holds more than the ceiling", () => {
    expect(groupOrder(reconcile(null, ["a", "b", "c", "d", "e", "f"]))).toHaveLength(MAX_GROUPS);
  });
});

describe("the stored form", () => {
  it("round-trips", () => {
    const layout = splitAt(splitAt(leaf("a"), "a", "b", "right"), "b", "c", "down");
    expect(fromWire(JSON.parse(JSON.stringify(toWire(layout))))).toEqual(layout);
  });

  it.each([
    ["a group named twice", { split: "row", children: [{ g: "a" }, { g: "a" }], sizes: [0.5, 0.5] }],
    ["an axis this build does not know", { split: "diagonal", children: [{ g: "a" }], sizes: [1] }],
    ["a split with nothing in it", { split: "row", children: [], sizes: [] }],
    ["more groups than the ceiling", { split: "row", children: ["a", "b", "c", "d", "e"].map((g) => ({ g })), sizes: [] }],
    ["not a layout at all", "row"],
    ["an empty group name", { g: "" }],
  ])("reads %s as no layout", (_label, raw) => {
    expect(fromWire(raw)).toBeNull();
  });

  it("reads stored shares that do not add up as equal shares", () => {
    const read = fromWire({ split: "row", children: [{ g: "a" }, { g: "b" }], sizes: [7] });
    expect(read?.kind === "split" && read.sizes).toEqual([0.5, 0.5]);
  });
});

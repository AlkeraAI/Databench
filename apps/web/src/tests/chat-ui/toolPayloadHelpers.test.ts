// The two folds every tool body shares. Both exist to keep a card faithful to
// the payload it was handed: a tool's own ordering is part of its answer, so
// grouping folds neighbors and nothing else, and a tally that reshuffled equal
// counts would let one answer read two ways on two runs.

import { describe, expect, it } from "vitest";

import { groupRuns, tally } from "../../../../../packages/ui/src/chat/tools/alkeraPayload";

interface Relation {
  name: string;
  kind: string;
}

const relation = (name: string, kind: string): Relation => ({ name, kind });
const kindOf = (item: Relation): string => item.kind;

describe("groupRuns", () => {
  it("folds a run of neighbors that share a key into one group", () => {
    const paths = ["src/a.ts", "src/b.ts", "notes/c.md"];
    expect(groupRuns(paths, (path) => path.split("/")[0])).toEqual([
      { key: "src", items: ["src/a.ts", "src/b.ts"] },
      { key: "notes", items: ["notes/c.md"] },
    ]);
  });

  it("opens a second group for a key that comes back after an interruption", () => {
    const paths = ["src/a.ts", "notes/c.md", "src/b.ts"];
    expect(groupRuns(paths, (path) => path.split("/")[0])).toEqual([
      { key: "src", items: ["src/a.ts"] },
      { key: "notes", items: ["notes/c.md"] },
      { key: "src", items: ["src/b.ts"] },
    ]);
  });

  it.each([
    ["a run split three ways", ["x", "x", "y", "z", "z", "y"]],
    ["a single key throughout", ["x", "x", "x"]],
    ["a key that never repeats", ["x", "y", "z"]],
    ["one item", ["x"]],
  ])("keeps every item, in the order the tool returned them, for %s", (_case, keys) => {
    const items = keys.map((key, index) => relation(`r${index}`, key));
    const flattened = groupRuns(items, kindOf).flatMap((group) => group.items);
    expect(flattened).toEqual(items);
  });

  it("names each group after the key its items share", () => {
    const items = [relation("orders", "table"), relation("orders_v", "view"), relation("events", "table")];
    expect(groupRuns(items, kindOf).map((group) => group.key)).toEqual(["table", "view", "table"]);
  });

  it("makes no group out of an empty payload", () => {
    expect(groupRuns([], kindOf)).toEqual([]);
  });
});

describe("tally", () => {
  it("counts each kind and leads with the most numerous", () => {
    const items = [
      relation("a", "view"),
      relation("b", "table"),
      relation("c", "table"),
      relation("d", "macro"),
      relation("e", "table"),
    ];
    expect(tally(items, kindOf)).toEqual([
      ["table", 3],
      ["view", 1],
      ["macro", 1],
    ]);
  });

  it.each([
    ["view", "table"],
    ["table", "view"],
  ])("breaks a tie for %s and %s the way the payload introduced them", (first, second) => {
    const items = [relation("a", first), relation("b", second), relation("c", first), relation("d", second)];
    expect(tally(items, kindOf)).toEqual([
      [first, 2],
      [second, 2],
    ]);
  });

  it("counts a payload of one kind once", () => {
    const items = [relation("a", "table"), relation("b", "table")];
    expect(tally(items, kindOf)).toEqual([["table", 2]]);
  });

  it("counts nothing in an empty payload", () => {
    expect(tally([], kindOf)).toEqual([]);
  });
});

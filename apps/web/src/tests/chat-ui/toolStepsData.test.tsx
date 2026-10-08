// The data-cluster adapters read the wire payloads the alkera data tools
// answer with: sql.schema's self-discriminating describe/list result, the blob
// family's handle facts, blob.derive's typed reshape params, and blob.profile's
// per-column summary with its non-finite wrapper values.

import { describe, expect, it } from "vitest";

import { renderBody, stepOf, toolPart } from "./_steps";

// -- sql.schema: the payload discriminates its own mode --------------------

const DESCRIBE_RESULT = {
  table: "orders",
  columns: [
    { name: "id", data_type: "NUMBER", nullable: false },
    { name: "amount", data_type: "FLOAT" },
  ],
};

const LIST_RESULT = {
  relations: [
    { urn: "urn:a", name: "analytics.orders", kind: "table" },
    { urn: "urn:b", name: "analytics.orders_v", kind: "view" },
    { urn: "urn:c", name: "raw.events", kind: "table" },
  ],
};

describe("sql.schema step", () => {
  it("detects a describe from the output columns", () => {
    const step = stepOf(toolPart("sql.schema", { input: { connection: "snowflake_prod" }, output: DESCRIBE_RESULT }));
    expect(step.verb).toBe("Described");
    expect(step.object).toBe("orders");
    expect(step.data).toEqual({ kind: "count", text: "2 columns, 1 not null" });
    expect(step.expanded).toBe(true);
  });

  it("keeps the describe object, with no column count, when the call errored", () => {
    const step = stepOf(
      toolPart("sql.schema", {
        state: "error",
        errorText: "fixture: the warehouse was unreachable",
        input: { mode: "describe", connection: "snowflake_prod", table: "orders" },
      }),
    );
    expect(step.verb).toBe("Could not describe");
    expect(step.object).toBe("orders");
    expect(step.data).toBeUndefined();
  });

  it("binds not null only where the schema does", () => {
    const body = renderBody(
      stepOf(toolPart("sql.schema", { input: { connection: "snowflake_prod" }, output: DESCRIBE_RESULT })),
    );
    const rows = [...body.querySelectorAll('[role="row"]')].map((row) =>
      [...row.querySelectorAll('[role="cell"]')].map((cell) => cell.textContent),
    );
    // An absent nullable field means unconstrained, never "not null".
    expect(rows).toEqual([
      ["id", "NUMBER", "not null"],
      ["amount", "FLOAT", ""],
    ]);
  });

  it("tallies a list by relation kind, most numerous first", () => {
    const step = stepOf(
      toolPart("sql.schema", { input: { connection: "snowflake_prod", mode: "list" }, output: LIST_RESULT }),
    );
    expect(step.verb).toBe("Listed relations on"); // pins-source: the head verb is user-facing copy; pinning it is this test's job
    expect(step.object).toBe("snowflake_prod");
    expect(step.data).toEqual({ kind: "count", text: "2 tables, 1 view" });
    expect(step.expanded).toBe(false);

    const empty = stepOf(toolPart("sql.schema", { input: { connection: "c" }, output: { relations: [] } }));
    expect(empty.data).toEqual({ kind: "count", text: "0 relations" });
  });

  it("groups relations by namespace, naming odd kinds", () => {
    const body = renderBody(stepOf(toolPart("sql.schema", { input: { connection: "c" }, output: LIST_RESULT })));
    const heads = [...body.querySelectorAll("[data-cap] > div > p")].map((head) => head.textContent);
    expect(heads).toEqual(["analytics", "raw"]);
    // A table is the kind a reader assumes, so only a view names its own.
    const rows = [...body.querySelectorAll("[data-cap] > div > div")].map((row) => row.textContent);
    expect(rows).toEqual(["orders", "orders_vview", "events"]);
  });
});

// -- blob.create / blob.delete ---------------------------------------------

describe("blob.delete step", () => {
  it("collapses a delete that happened and states what it freed", () => {
    const step = stepOf(
      toolPart("blob.delete", { input: { handle: "a".repeat(64) }, output: { deleted: true, freed_bytes: 86016 } }),
    );
    expect(step.verb).toBe("Deleted");
    expect(step.data).toEqual({ kind: "count", text: "freed 84 KB" });
    expect(step.expanded).toBe(false);
  });

  it("opens a delete that did not happen", () => {
    const step = stepOf(
      toolPart("blob.delete", {
        input: { handle: "a".repeat(64) },
        output: { deleted: false, freed_bytes: 0, message: "Another chat still references this result." },
      }),
    );
    expect(step.data).toEqual({ kind: "count", text: "nothing freed" });
    expect(step.expanded).toBe(true);
    expect(renderBody(step).textContent).toContain("Another chat still references this result.");
  });
});

describe("blob.create step", () => {
  it("infers kind and extent before the call answers", () => {
    const text = stepOf(
      toolPart("blob.create", { state: "running", input: { text: "hello world", result_name: "notes" } }),
    );
    // Still out: the verb is in the present and no extent figure rides the head.
    expect(text.verb).toBe("Storing");
    expect(text.object).toBe("notes");
    expect(text.data).toBeUndefined();
    expect(renderBody(text).querySelector(".chat-blob-kind")?.textContent).toBe("text");

    const tabular = stepOf(
      toolPart("blob.create", {
        state: "running",
        input: { rows: [[1], [2], [3]], columns: ["a"], result_name: "sample" },
      }),
    );
    expect(tabular.data).toBeUndefined();
    expect(renderBody(tabular).querySelector(".chat-blob-kind")?.textContent).toBe("rows");
  });
});

// -- blob.derive: the reshape params as the SELECT they compile to ---------

function deriveLines(input: Record<string, unknown>): { keyword: string; argument: string }[] {
  const body = renderBody(stepOf(toolPart("blob.derive", { input: { handle: "b".repeat(64), ...input } })));
  // The band opens with the source line, so the clauses are the paragraphs after it.
  return [...body.querySelectorAll("p")].slice(1).map((line) => {
    const parts = [...line.querySelectorAll("span")].map((part) => part.textContent ?? "");
    return { keyword: parts[0] ?? "", argument: parts[1] ?? "" };
  });
}

describe("blob.derive recipe", () => {
  it("compiles the params in SQL clause order", () => {
    expect(
      deriveLines({
        select_columns: ["region", "total"],
        distinct: true,
        where: "total > 100",
        order_by: ["total"],
        descending: true,
        limit: 10,
      }),
    ).toEqual([
      { keyword: "select distinct", argument: "region, total" },
      { keyword: "where", argument: "total > 100" },
      { keyword: "order by", argument: "total desc" },
      { keyword: "limit", argument: "10" },
    ]);

    expect(deriveLines({})).toEqual([{ keyword: "select", argument: "*" }]);

    // The flags read as words, so a false one must not print its word.
    expect(deriveLines({ select_columns: ["a"], order_by: ["a"], distinct: false, descending: false })).toEqual([
      { keyword: "select", argument: "a" },
      { keyword: "order by", argument: "a" },
    ]);
  });

  it("speaks a derive as Reshaped over the short handle", () => {
    const step = stepOf(toolPart("blob.derive", { input: { handle: "b".repeat(64) } }));
    expect(step.verb).toBe("Reshaped");
    expect(step.object).toBe("b".repeat(12));
  });
});

// -- blob.profile: values as the payload words them ------------------------

const PROFILE_RESULT = {
  row_count: 100,
  columns: [
    {
      name: "score",
      dtype: "float64",
      null_count: 5,
      distinct_count: 42,
      min: { $nonfinite: "-inf" },
      max: { $nonfinite: "inf" },
      top_k: [
        { value: { $nonfinite: "nan" }, count: 7 },
        { value: 3.5, count: 4 },
        { value: null, count: 2 },
      ],
    },
  ],
};

describe("blob.profile step", () => {
  it("renders a non-finite marker word verbatim", () => {
    const body = renderBody(
      stepOf(toolPart("blob.profile", { input: { handle: "c".repeat(64) }, output: PROFILE_RESULT })),
    );
    expect(body.textContent).toContain("min-infmaxinf");
    const tops = [...body.querySelectorAll("section > div > div")].map((row) => row.querySelector("span")?.textContent);
    expect(tops).toEqual(["nan", "3.5", "null"]);
  });

  it("figures the summary as columns over rows", () => {
    const step = stepOf(toolPart("blob.profile", { input: { handle: "c".repeat(64) }, output: PROFILE_RESULT }));
    expect(step.verb).toBe("Profiled");
    expect(step.data).toEqual({ kind: "count", text: "1 column over 100 rows" });
    expect(step.expanded).toBe(true);
  });

  it("draws the completeness bar only where a column has holes", () => {
    const holed = renderBody(
      stepOf(toolPart("blob.profile", { input: { handle: "c".repeat(64) }, output: PROFILE_RESULT })),
    );
    expect(holed.querySelector("section > span")).not.toBeNull();
    const clean = {
      row_count: 100,
      columns: [{ name: "id", dtype: "int64", null_count: 0, distinct_count: 100, min: 1, max: 100, top_k: [] }],
    };
    const body = renderBody(stepOf(toolPart("blob.profile", { input: { handle: "c".repeat(64) }, output: clean })));
    expect(body.querySelector("section > span")).toBeNull();
  });
});

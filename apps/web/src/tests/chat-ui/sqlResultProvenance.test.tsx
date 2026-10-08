// What a SQL result says about itself, and what a reader can do with it.
//
// A table of numbers is only worth something with its provenance beside it, so
// the engine, the run time, the row count and the duration are asserted as
// rendered TEXT in the card's own body — the same body the card opens by
// default, which is what "without a click" means here.
//
// What a reader can do with it is the other half: saved queries and saved
// results were retired, so a card that still offered them would be offering an
// action that goes nowhere.

import { describe, expect, it } from "vitest";

import { renderBody, stepOf, toolPart } from "./_steps";

// The shape `sql.query` returns today: the provenance rides in its own block
// beside the rows, and the card reads the five facts from there.
const RESULT = {
  columns: ["day", "orders"],
  preview_rows: [
    ["2026-09-05", 12],
    ["2026-09-06", 17],
  ],
  row_count: 2,
  provenance: {
    connection_id: "rec-42",
    connection_name: "prod-warehouse",
    role: "analytics_readonly",
    engine: "postgres",
    executed_at: "2026-09-06T12:00:00+00:00",
    duration_ms: 412,
    row_count: 2,
    sql: "select day, orders from daily",
  },
};

// A result recorded before the tool carried a block: the same facts flat on
// the output, or not there at all.
const FLAT_RESULT = {
  columns: ["day", "orders"],
  preview_rows: [
    ["2026-09-05", 12],
    ["2026-09-06", 17],
  ],
  row_count: 2,
  engine: "postgres",
  executed_at: "2026-09-06T12:00:00Z",
  duration_ms: 412,
};

const INPUT = { sql: "select day, orders from daily", connection: "prod-warehouse" };

const resultPart = () => toolPart("sql_query", { input: INPUT, output: RESULT });

describe("a SQL result's provenance", () => {
  it("opens expanded, so its table and statement are read without a click", () => {
    expect(stepOf(resultPart()).expanded).toBe(true);
  });

  it.each([
    ["the connection it read", "prod-warehouse"],
    ["the engine that ran it", "postgres"],
    ["the role it ran as", "analytics_readonly"],
    ["the statement", "select day, orders from daily"],
    ["how many rows came back", "2 rows"],
    ["how long it took", "412 ms"],
  ])("shows %s", (_case, text) => {
    const body = renderBody(stepOf(resultPart()));
    expect(body.textContent).toContain(text);
  });

  it("names the source as one thing: connection, engine, role", () => {
    const body = renderBody(stepOf(resultPart()));
    const source = body.querySelector("[data-source]");
    expect(source?.textContent?.replace(/\s+/g, " ").trim()).toBe("prod-warehouse· postgres· as analytics_readonly");
  });

  it("shows when it ran, in the reader's own clock", () => {
    const body = renderBody(stepOf(resultPart()));
    // Rendered through the reader's locale, so the assertion is on the day it
    // names rather than on a formatting the test would be pinning for itself.
    expect(body.textContent).toMatch(/2026/);
  });

  it("shows the preview rows themselves", () => {
    const body = renderBody(stepOf(resultPart()));
    expect(body.textContent).toContain("2026-09-05");
    expect(body.textContent).toContain("17");
  });

  it("counts the rows the statement returned, not the rows previewed", () => {
    const output = { ...RESULT, provenance: { ...RESULT.provenance, row_count: 120 }, row_count: 120 };
    const body = renderBody(stepOf(toolPart("sql_query", { input: INPUT, output })));
    expect(body.textContent).toContain("120 rows");
  });

  it("reads the same facts off a result recorded flat, before the tool carried a block", () => {
    const body = renderBody(stepOf(toolPart("sql_query", { input: INPUT, output: FLAT_RESULT })));
    for (const text of ["prod-warehouse", "postgres", "2 rows", "412 ms"]) {
      expect(body.textContent).toContain(text);
    }
    expect(body.textContent).not.toContain(" as ");
  });

  it("says nothing about an engine, a role or a duration the call did not report", () => {
    const body = renderBody(
      stepOf(toolPart("sql_query", { input: INPUT, output: { columns: [], preview_rows: [], row_count: 0 } })),
    );
    expect(body.textContent).not.toContain("ms");
    expect(body.textContent).not.toContain("postgres");
    expect(body.textContent).not.toContain(" as ");
  });
});

describe("what a result card offers to do with a result", () => {
  // Saved queries and saved results were retired: the routes behind them answer
  // 410, so a card that still offered them would be offering a dead end.
  it.each([
    ["a statement", resultPart()],
    ["a whole table", toolPart("sql_query", { input: { table: "orders" }, output: RESULT })],
  ])("keeps no Save control on a result read from %s", (_label, part) => {
    const body = renderBody(stepOf(part));

    expect(body.querySelector(".chat-tool-action")).toBeNull();
    expect(
      [...body.querySelectorAll("button")].map((button) => button.textContent ?? ""),
    ).not.toContainEqual(expect.stringMatching(/^Save as/));
  });
});

describe("a query that failed", () => {
  const failedPart = () =>
    toolPart("sql_query", {
      input: INPUT,
      output: null,
      state: "error",
      errorText: 'Query failed: relation "daily" does not exist',
    });

  it("counts no rows and no columns in its step, and says it could not query", () => {
    const step = stepOf(failedPart());
    expect(step.data).toBeUndefined();
    expect(step.verb).toBe("Could not query");
  });

  it("shows the statement it ran and no empty-result caption", () => {
    const html = renderBody(stepOf(failedPart())).textContent ?? "";
    expect(html).toContain("select day, orders from daily");
    expect(html).not.toContain("The query returned no rows.");
    expect(html).not.toMatch(/\b0 rows\b/);
  });
});

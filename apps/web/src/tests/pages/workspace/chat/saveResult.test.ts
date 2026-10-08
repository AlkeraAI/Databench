// Turning a result the agent produced into something the org keeps.
//
// The parameter cases are the load-bearing ones: a saved query is only reusable
// if the slots it was written with become named parameters, and only SAFE if a
// slot that names nothing is left alone rather than promoted into a parameter
// the re-run then has to invent a value for.
//
// "A slot" means exactly what the compiler means by it. The server's own
// grammar is `{name}` / `{name.start}` / `{name.end}`, so anything else in
// braces — a JSON literal, a native ClickHouse `{x:Type}` placeholder, a
// spaced `{ name }` — is not a parameter here either. Declaring one the
// template will never bind puts a field on the re-run form that changes
// nothing, which is the same class of lie as leaving one out.

import { describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";

import {
  columnsOf,
  eventIdOf,
  paramsOf,
  querySpecOf,
  sqlOf,
  suggestedTitle,
} from "@/pages/workspace/chat/saveResult";

const CHAT_ID = "5f1f0a3c-2f0e-4a2b-9d5c-71a3c9f0b111";

function part(over: Partial<ToolConversationPart> = {}): ToolConversationPart {
  return {
    id: "p1",
    kind: "tool",
    callId: "call-1",
    name: "sql_query",
    state: "completed",
    input: { sql: "select 1", connection: "warehouse" },
    output: { result: { columns: ["day", "orders"], row_count: 2, engine: "postgres" } },
    ...over,
  } as ToolConversationPart;
}

describe("what a result offers to save", () => {
  it("names the call as the event the machine uploads the payload for", () => {
    expect(eventIdOf(part())).toBe("call-1");
  });

  it("takes the preview's columns as the saved result's columns", () => {
    expect(columnsOf(part())).toEqual([
      { name: "day", label: null },
      { name: "orders", label: null },
    ]);
  });

  it("has no columns to offer when the call returned none", () => {
    expect(columnsOf(part({ output: { result: {} } }))).toEqual([]);
  });

  it.each([
    ["the tool's own result name", { result: { result_name: "Daily orders" } }, {}, "Daily orders"],
    ["the relation a table-mode call read", { result: {} }, { table: "orders" }, "orders"],
    ["nothing to suggest", { result: {} }, {}, ""],
  ])("suggests a title from %s", (_case, output, input, expected) => {
    expect(suggestedTitle(part({ output, input }))).toBe(expected);
  });

  it("reads the statement from either spelling the tool uses", () => {
    expect(sqlOf(part({ input: { sql: "select a" } }))).toBe("select a");
    expect(sqlOf(part({ input: { query: "select b" } }))).toBe("select b");
  });
});

describe("the parameters a saved query declares", () => {
  it.each([
    [
      "one slot",
      "select * from o where customer = {customer}",
      [{ name: "customer", type: "string" }],
    ],
    [
      "several, in the order they appear",
      "select * from o where customer = {customer} and day >= {since}",
      [
        { name: "customer", type: "string" },
        { name: "since", type: "string" },
      ],
    ],
    ["a slot used twice, kept once", "where a = {c} or b = {c}", [{ name: "c", type: "string" }]],
    ["no slots at all", "select 1", []],
    [
      "a range read at both ends, declared once",
      "where day between {period.start} and {period.end}",
      [{ name: "period", type: "daterange" }],
    ],
    [
      "a range whose name is also used bare",
      "where day = {period} or day between {period.start} and {period.end}",
      [{ name: "period", type: "daterange" }],
    ],
  ])("%s", (_case, sql, expected) => {
    expect(paramsOf(sql)).toEqual(expected);
  });

  it.each([
    ["an empty slot", "select {} from t"],
    ["a whitespace-only slot", "select {   } from t"],
    // The compiler's grammar has no spaces in it, so this is NOT a slot: the
    // template would bind nothing and the form would ask for nothing useful.
    ["a spaced slot the compiler does not read", "where a = { customer }"],
    ["a native ClickHouse placeholder the driver binds itself", "where a = {c:String}"],
    ["a JSON literal in the statement", "select '{\"a\": 1}'::jsonb"],
    ["a slot that starts with a digit", "where a = {1st}"],
    ["a mixed-case slot, which cannot be DECLARED", "where a = {Customer}"],
  ])("%s is not a declared parameter", (_case, sql) => {
    expect(paramsOf(sql)).toEqual([]);
  });

  it("does not read across a closing brace into the next slot", () => {
    expect(paramsOf("{a} and {b}")).toEqual([
      { name: "a", type: "string" },
      { name: "b", type: "string" },
    ]);
  });
});

describe("the spec a saved query is stored as", () => {
  it("uses the field names the server declares, and carries no result data", () => {
    const spec = querySpecOf(
      part({ input: { sql: "select * from o where c = {customer}", connection: "warehouse" } }),
      CHAT_ID,
    );
    expect(spec).toEqual({
      sql_template: "select * from o where c = {customer}",
      params: [{ name: "customer", type: "string" }],
      connection_id: null,
      source_chat_id: CHAT_ID,
      engine: "postgres",
    });
    expect(Object.keys(spec)).not.toContain("rows");
  });

  it.each([
    ["sql", "the statement under a name the spec does not declare"],
    ["parameters", "a bare list of names"],
    ["connection", "a connection under the wrong key"],
  ])("never sends %s — %s", (key) => {
    const spec = querySpecOf(part(), CHAT_ID);
    expect(Object.keys(spec)).not.toContain(key);
  });

  it("names the chat it was saved out of, because that is where a re-run runs", () => {
    expect(querySpecOf(part(), CHAT_ID).source_chat_id).toBe(CHAT_ID);
  });

  it("says it has no chat rather than inventing one", () => {
    expect(querySpecOf(part(), undefined).source_chat_id).toBeNull();
  });

  it("takes the connection id from the tool's provenance when it has one", () => {
    const spec = querySpecOf(
      part({ output: { result: { engine: "tinybird", connection_id: "conn-7" } } }),
      CHAT_ID,
    );
    expect(spec.connection_id).toBe("conn-7");
    expect(spec.engine).toBe("tinybird");
  });

  it("leaves the engine to the server when the tool named none", () => {
    const spec = querySpecOf(part({ output: { result: {} }, input: { sql: "select 1" } }), CHAT_ID);
    expect(Object.keys(spec)).not.toContain("engine");
  });
});

// `call_tool` is the generic MCP envelope: the tool actually being called is
// named in `input.name` and its arguments in `input.args`, so a card can only
// route the call once the envelope is peeled and the inner tool's identity is
// promoted onto the part.
//
// Deciding that a part IS the envelope by substring re-identified every tool
// whose wire name merely contains the word -- `batch_call_tool` rendered as
// whatever it happened to be batching, its own arguments gone. Nothing catches
// that downstream: the promoted part is a well-formed tool part, just a
// different tool's. So the envelope is matched by canonical name, and every
// case below builds its part with a REAL envelope's `input`, leaving the name
// as the only thing that decides.

import { describe, expect, it } from "vitest";

import type { ToolConversationPart } from "./conversation";

import { unwrapCallTool, unwrapToolResult } from "./toolResult";
import vectors from "./toolResultVectors.json";

const WRAPPER_INPUT = { name: "sql.query", args: { sql: "select 1" } };

/** The envelope's result as it reaches the frontend. The loopback MCP server
 *  serializes an alkera tool's return into one text block, so what arrives is a
 *  JSON string rather than an object. */
const WRAPPED_OUTPUT = JSON.stringify({ result: { rows: 1 } });

function toolPart(overrides: Partial<ToolConversationPart> = {}): ToolConversationPart {
  return {
    id: "part-1",
    kind: "tool",
    callId: "call-1",
    name: "call_tool",
    state: "completed",
    input: WRAPPER_INPUT,
    output: WRAPPED_OUTPUT,
    ...overrides,
  };
}

describe("a tool whose name merely contains call_tool keeps its own identity", () => {
  it.each([
    "batch_call_tool",
    "call_tools",
    "call_tool_async",
    "mcp__alkera__batch_call_tool",
    "alkera_batch_call_tool",
    "Batch_Call_Tool",
  ])("%s is not the envelope", (name) => {
    const part = toolPart({ name });
    const unwrapped = unwrapCallTool(part);

    expect(unwrapped.name).toBe(name);
    expect(unwrapped.input).toEqual(WRAPPER_INPUT);
    expect(unwrapped.output).toBe(WRAPPED_OUTPUT);
    expect(unwrapped).toEqual(part);
  });
});

describe("every canonical spelling of the envelope unwraps to the inner tool", () => {
  it.each([
    "call_tool",
    "mcp__alkera__call_tool", // claude's composition
    "alkera_call_tool", // opencode's loopback prefix
    "alkera-call_tool", // the historical hyphenated prefix
    "Call_Tool", // a model echoing its trained-on capitalization
    "mcp__alkera__Call_Tool",
    "  call_tool  ",
  ])("%s", (name) => {
    const unwrapped = unwrapCallTool(toolPart({ name }));

    expect(unwrapped.name).toBe("sql.query");
    expect(unwrapped.input).toEqual({ sql: "select 1" });
    expect(unwrapped.output).toEqual({ rows: 1 });
  });

  it("the promoted part keeps the call it belongs to and how it ended", () => {
    const unwrapped = unwrapCallTool(
      toolPart({ id: "p-9", callId: "c-9", state: "error", errorText: "boom" }),
    );

    expect(unwrapped).toMatchObject({
      id: "p-9",
      callId: "c-9",
      state: "error",
      errorText: "boom",
      name: "sql.query",
    });
  });

  it("an envelope carrying no args promotes the inner name with none", () => {
    const unwrapped = unwrapCallTool(toolPart({ input: { name: "sql.query" } }));

    expect(unwrapped.name).toBe("sql.query");
    expect(unwrapped.input).toEqual({});
  });
});

describe("an envelope with no inner name to promote stays untouched", () => {
  it.each<[string, ToolConversationPart["input"]]>([
    ["no input", undefined],
    ["input with no keys", {}],
    ["an empty inner name", { name: "", args: { sql: "select 1" } }],
    ["a non-string inner name", { name: 42, args: { sql: "select 1" } }],
    ["args but no name", { args: { sql: "select 1" } }],
  ])("%s", (_case, input) => {
    const part = toolPart({ input });

    expect(unwrapCallTool(part)).toEqual(part);
  });
});

describe("the envelope's result is unwrapped, and only when it is an envelope", () => {
  it.each<[string, string]>([
    ["result", JSON.stringify({ result: { rows: 1 } })],
    ["structuredContent", JSON.stringify({ structuredContent: { rows: 1 } })],
    ["a content text block", JSON.stringify({ content: [{ text: JSON.stringify({ rows: 1 }) }] })],
  ])("%s reaches the inner tool's card as the value it returned", (_case, output) => {
    expect(unwrapCallTool(toolPart({ output })).output).toEqual({ rows: 1 });
  });

  it("bare tool text is not a wrapped value and stays the string it was", () => {
    expect(unwrapCallTool(toolPart({ output: "ran 1 query" })).output).toBe("ran 1 query");
  });
});

// The same cases every server-side reader of a tool result is held to, so a
// result is one value everywhere.
describe("a tool output unwraps to the value the tool returned", () => {
  const cases = vectors.cases as { id: string; output: unknown; unwrapped?: unknown }[];
  it.each(cases.map((c) => [c.id, c] as const))("%s", (_id, c) => {
    expect(unwrapToolResult(c.output)).toEqual(c.unwrapped);
  });
});

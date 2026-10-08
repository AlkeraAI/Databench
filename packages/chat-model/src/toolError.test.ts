import { describe, expect, it } from "vitest";

import { asSentence, isRefusal, parseToolError, readableToolError } from "./toolError";

const ENVELOPE = JSON.stringify({
  error: "permission denied: sharing this knowledge item was not approved",
  tool: "context_note",
  classification: "error",
});

describe("a failed tool call's envelope", () => {
  it("reads the message, the tool and the classification off the CLI's envelope", () => {
    expect(parseToolError(ENVELOPE)).toEqual({
      message: "permission denied: sharing this knowledge item was not approved",
      tool: "context_note",
      classification: "error",
    });
  });

  it("allows the CLI's extra keys and surrounding whitespace", () => {
    const text = `  ${JSON.stringify({ error: "boom", tool: "t", classification: "timeout", preview: "x" })}\n`;
    expect(parseToolError(text)?.message).toBe("boom");
    expect(parseToolError(text)?.classification).toBe("timeout");
  });

  it.each([
    ["a missing key", JSON.stringify({ error: "boom", tool: "t" })],
    ["a mistyped key", JSON.stringify({ error: "boom", tool: 1, classification: "error" })],
    ["a message that is not a string", JSON.stringify({ error: ["boom"], tool: "t", classification: "error" })],
    ["a JSON array", JSON.stringify([{ error: "boom", tool: "t", classification: "error" }])],
    ["a prefixed envelope", `Error: ${ENVELOPE}`],
    ["a tool's own plain text", "permission denied: sharing this knowledge item was not approved"],
    ["the harness's abort sentence", "Tool execution aborted"],
    ["broken JSON", "{\"error\": "],
    ["an empty string", ""],
  ])("is not read into %s", (_what, text) => {
    expect(parseToolError(text)).toBeNull();
  });

  it("gives a reader the message for an envelope and the text itself for anything else", () => {
    // The envelope's message, read as a sentence: the first letter raised and nothing else.
    expect(readableToolError(ENVELOPE)).toBe(
      "Permission denied: sharing this knowledge item was not approved",
    );
    expect(readableToolError("Tool execution aborted")).toBe("Tool execution aborted");
    expect(readableToolError(null)).toBeNull();
    expect(readableToolError(undefined)).toBeNull();
    expect(readableToolError("")).toBeNull();
  });
});


describe("a failed call's line reads as a sentence", () => {
  it.each([
    ["query failed: relation \"t\" does not exist", "Query failed: relation \"t\" does not exist"],
    ["permission denied: bash", "Permission denied: bash"],
    ["  invalid blob handle: x", "  Invalid blob handle: x"],
  ])("raises the first letter of %j", (given, expected) => {
    expect(asSentence(given)).toBe(expected);
    expect(readableToolError(given)).toBe(expected);
  });

  it.each([
    "web.fetch failed for https://example.com",
    "files.lease_mismatch: the folder moved",
    "sql.query: two legal call signatures",
    "/etc/shadow is not readable",
    "https://example.com answered 500",
    "Already a sentence.",
    "42 rows is too many",
  ])("leaves %j as it came", (given) => {
    expect(asSentence(given)).toBe(given);
  });

  it("raises the envelope's message, not the envelope", () => {
    const text = JSON.stringify({ error: "query failed: boom", tool: "sql.query", classification: "provider" });
    expect(readableToolError(text)).toBe("Query failed: boom");
  });
});


describe("what counts as a refusal", () => {
  it.each([
    ["permission denied: connection 'pg' is read-only by its nature", true],
    ["Permission was not granted for this call.", true],
    ["Refused: this chat is in read-only mode", true],
    ["The workspace policy refused this call.", true],
    [JSON.stringify({ error: "permission denied: not approved", tool: "bash", classification: "error" }), true],
    // A database saying no is the run's own failure, not the gate's refusal.
    ["permission denied for relation orders", false],
    ["permission denied for table orders", false],
    ["psql: connection refused", false],
    ["", false],
    [null, false],
  ] as const)("%s → %s", (text, expected) => {
    expect(isRefusal(text)).toBe(expected);
  });
});

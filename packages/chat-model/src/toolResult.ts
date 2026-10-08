// Unwrapping a tool result to the value the tool actually returned.
//
// The loopback MCP server serializes an alkera tool's result into ONE text block
// (`mcp_server.py`), so a result reaches the frontend as a JSON STRING rather
// than an object. On top of that a result may be wrapped: `call_tool` nests the
// inner tool's return under `result`, and the MCP spec allows `structuredContent`
// or a `content[]` array of text blocks. An opencode-native tool returns bare
// text and unwraps to nothing.
//
// This lives in the model package because every reader needs it -- the card
// registry, the step adapters, and the webview's event fold -- and must unwrap
// the same envelopes whichever reader sees a call first.

import type { ToolConversationPart } from "./conversation";
import { canonicalToolName } from "./toolNames";

/** How deep a wrapper chain may nest before we stop looking. */
const MAX_DEPTH = 4;

function parsed(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return undefined;
  }
}

/** The value a tool returned, with any JSON-string and envelope layers removed.
 *  `undefined` when the output is absent or is text that is not a wrapper --
 *  bare tool text is not a wrapped value, so a caller keeps its own raw string. */
export function unwrapToolResult(output: unknown, depth = 0): unknown {
  if (depth > MAX_DEPTH) return undefined;
  const value = parsed(output);
  if (!value || typeof value !== "object" || Array.isArray(value)) return value ?? undefined;
  const obj = value as Record<string, unknown>;
  // `result` is an envelope only when it is the object's WHOLE answer. A tool's
  // own payload can carry a `result` field beside its peers (snowflake's
  // cancel_query returns `{query_id, result}`), and unwrapping that ate the
  // record and rendered the call as a failure.
  if (obj.result !== undefined && Object.keys(obj).length === 1) {
    return unwrapToolResult(obj.result, depth + 1);
  }
  if (obj.structuredContent !== undefined) return unwrapToolResult(obj.structuredContent, depth + 1);
  if (Array.isArray(obj.content)) {
    for (const block of obj.content) {
      const text = (block as { text?: unknown } | null)?.text;
      if (typeof text !== "string") continue;
      const inner = unwrapToolResult(text, depth + 1);
      if (inner !== undefined) return inner;
    }
  }
  return obj;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/** The unwrapped result as the record a payload reader wants, or null. */
export function toolResultRecord(output: unknown): Record<string, unknown> | null {
  const value = unwrapToolResult(output);
  return isRecord(value) ? value : null;
}

/** A value as the record it is, parsing a JSON string but unwrapping NOTHING --
 *  for a payload whose own top-level keys are the answer. */
export function parseRecord(value: unknown): Record<string, unknown> | null {
  const out = parsed(value);
  return isRecord(out) ? out : null;
}

/** Return the inner tool payload from a generic `call_tool` envelope. The
 *  wrapper is matched by its canonical name, never by substring: a tool that
 *  merely contains the words (`batch_call_tool`) keeps its own identity. */
export function unwrapCallTool(part: ToolConversationPart): ToolConversationPart {
  if (canonicalToolName(part.name).toLowerCase() !== "call_tool") return part;
  const input = part.input ?? {};
  const innerName = typeof input.name === "string" ? input.name : "";
  if (!innerName) return part;
  const innerArgs =
    input.args && typeof input.args === "object" && !Array.isArray(input.args)
      ? (input.args as Record<string, unknown>)
      : {};
  // The wrapper's result arrives as a JSON STRING from the loopback MCP server,
  // so the shared unwrap runs before the inner tool's card reads it. Text that
  // is not a wrapper stays the inner tool's own output, untouched.
  const inner = unwrapToolResult(part.output);
  const output = isRecord(inner) ? inner : part.output;
  return { ...part, name: innerName, input: innerArgs, output };
}

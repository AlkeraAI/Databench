// A tool call reaches the transcript under whichever spelling its harness
// composed. Opencode advertises an MCP tool as `<client>_<tool>` with every
// character outside `[A-Za-z0-9_-]` replaced (`sanitize` and the composition loop
// in vendor/opencode/packages/opencode/src/mcp/index.ts), so `blob.profile`
// arrives as `alkera_blob_profile`; claude keeps the dots behind `mcp__alkera__`.
// Most of the manifest is dotted, so a step registry that matches only the dotted
// spelling hands most of the tool surface to the generic ledger under opencode.
//
// The step registry also has to resolve the spelling AWAY before the adapter
// reads it: the spec sheet takes its vendor, and so the brand mark it wears, from
// the head of a dotted name. `snowflake_running_queries` has no head to split.
//
// The two compositions below are the HARNESSES' rules, restated here on purpose.
// Asking the registry to build its own input would prove nothing about the wire.
// The card half of the same contract is pinned in
// packages/ui/src/chat/tool-cards/toolSpellings.test.ts.

import { isValidElement } from "react";
import { describe, expect, it } from "vitest";

import { ALKERA_TOOL_NAMES } from "@alkera/chat-model";
import { resolveStep, type CardStep } from "@alkera/ui";
import { ConnectorMark } from "@alkera/ui";

import { toolPart } from "./_steps";

/** Opencode's composition: the MCP client name, an underscore, and the tool name
 *  with every character outside its allowed class replaced. */
function opencode(name: string): string {
  return `alkera_${name.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}

/** Claude's composition: the server prefix, the tool's own name untouched. */
function claude(name: string): string {
  return `mcp__alkera__${name}`;
}

const NAMES = [...ALKERA_TOOL_NAMES];

/** One call, spelled three ways. The id is pinned so the only difference between
 *  two of these parts is the wire name -- everything the adapter derives has to
 *  come out identical. */
function stepFor(wire: string): CardStep | null {
  return resolveStep(
    toolPart(wire, {
      id: "p1",
      callId: "c1",
      input: { path: "warehouse/orders.sql" },
      output: JSON.stringify({ rows: [{ id: 1, name: "orders" }] }),
    }),
  );
}

/** The vendor whose mark the step wears, or null for a step with a drawn glyph. */
function markOf(step: CardStep | null): string | null {
  const glyph = step?.glyph;
  if (!isValidElement(glyph) || glyph.type !== ConnectorMark) return null;
  return (glyph.props as { id: string }).id;
}

describe("tool spellings", () => {
  it("renders the same step whichever harness spelled it", () => {
    expect(NAMES.length).toBeGreaterThan(1);
    // Adapter identity and every fact the adapter derives from the call, in one
    // comparison: a step routed to the wrong adapter, or routed to the right one
    // under an unresolved name, differs somewhere in here.
    for (const name of NAMES) {
      const step = stepFor(name);
      expect(stepFor(opencode(name)), `${name} under opencode's spelling`).toEqual(step);
      expect(stepFor(claude(name)), `${name} under claude's spelling`).toEqual(step);
    }
  });

  it("wears the vendor's mark under either spelling", () => {
    // Equality above says the spellings agree; this says what they agree ON. A
    // vendor's mark exists only because the adapter could split a dotted head off
    // `part.name`, so it is the visible proof that the sanitized spelling was
    // resolved rather than merely tolerated.
    const branded = NAMES.filter((name) => markOf(stepFor(name)) !== null);
    expect(new Set(branded.map((name) => name.split(".")[0])).size).toBeGreaterThan(1);
    for (const name of branded) {
      expect(markOf(stepFor(opencode(name))), `${name} under opencode's spelling`).toBe(name.split(".")[0]);
      expect(markOf(stepFor(claude(name))), `${name} under claude's spelling`).toBe(name.split(".")[0]);
    }
  });

  it("reaches the generic ledger under an unknown tool's own name", () => {
    // The manifest lags a tool the backend has already shipped, and that call
    // still has to read as itself: the harness prefix goes, the casing stays.
    expect(stepFor("alkera_MysteryTool")?.object).toBe("MysteryTool");
    expect(stepFor("mcp__alkera__MysteryTool")?.object).toBe("MysteryTool");
  });
});

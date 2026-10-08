// A call that has not answered yet must not be drawn as one that has: an
// in-flight tool, whose arguments are still streaming and whose output does not
// exist, shows no `{}` band, no bare `null`, no past-tense verb and no "Hide
// result" disclosure. These fail if either guard is removed.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";

import { resolveStep } from "./steps";
import type { CardStep } from "./step";

let minted = 0;

function part(over: Partial<Omit<ToolConversationPart, "kind">> = {}): ToolConversationPart {
  minted += 1;
  return { id: `part-${minted}`, kind: "tool", callId: `call-${minted}`, name: "call_tool", state: "completed", ...over };
}

function stepOf(over: Partial<Omit<ToolConversationPart, "kind">> = {}): CardStep {
  const step = resolveStep(part(over));
  if (!step) throw new Error("resolveStep returned no step");
  return step;
}

function bodyText(step: CardStep): string {
  return render(<div className="chat-root">{step.body}</div>).container.textContent ?? "";
}

describe("a generic tool call still in flight", () => {
  for (const state of ["pending", "running"] as const) {
    it(`says it is calling, and shows no result, while ${state}`, () => {
      const step = stepOf({ state });
      expect(step.verb).toBe("Calling");
      expect(step.status).toBe(state === "running" ? "running" : "pending");
      // No interior at all: nothing is known yet, so there is nothing to disclose.
      expect(step.body).toBeUndefined();
      expect(step.data).toBeUndefined();
    });
  }

  it("never prints an empty argument object as a row", () => {
    const step = stepOf({ state: "running", input: {} });
    expect(step.body).toBeUndefined();
  });

  it("shows the arguments that have landed, and still no result", () => {
    const step = stepOf({ state: "running", name: "mystery_tool", input: { table: "orders" } });
    const text = bodyText(step);
    expect(text).toContain("orders");
    expect(text).not.toContain("null");
    expect(step.verb).toBe("Calling");
  });

  // The wrapper's own name is what a reader sees ONLY while the envelope is
  // nameless. Once it names its target the router hands the call to that tool's
  // own card, under that tool's own name -- so there is no "via call_tool" form
  // to render, and the envelope never outlives the moment it is unresolved.
  it("gives way to the target's own card once the envelope names one", () => {
    expect(stepOf({ state: "running" }).object).toBe("call_tool");

    const resolved = stepOf({ state: "running", input: { name: "warehouse.snapshot", args: { day: "friday" } } });
    expect(resolved.object).toBe("warehouse.snapshot");
    expect(resolved.verb).toBe("Calling");
    // The inner arguments are the call's, not the envelope's.
    expect(bodyText(resolved)).toContain("friday");
    expect(bodyText(resolved)).not.toContain("warehouse.snapshot");
  });
});

describe("a generic tool call that has answered", () => {
  it("renders the result it returned", () => {
    const step = stepOf({ name: "mystery_tool", input: { table: "orders" }, output: { rows: 7, status: "ok" } });
    const text = bodyText(step);
    expect(step.verb).toBe("Called");
    expect(text).toContain("rows");
    expect(text).toContain("7");
  });

  it("says the call returned nothing rather than printing null", () => {
    const step = stepOf({ name: "mystery_tool", input: { table: "orders" }, output: null });
    const text = bodyText(step);
    expect(text).not.toContain("null");
    expect(text).toContain("This call returned nothing.");
  });

  it("says the same when no output field ever arrived", () => {
    const step = stepOf({ name: "mystery_tool", input: { table: "orders" } });
    expect(bodyText(step)).toContain("This call returned nothing.");
  });

  // An empty container is not a result either: an empty ledger under a
  // "0 fields" figure claims an answer the card does not have.
  // The loopback MCP server hands a result over as a JSON string, so a card
  // meets both the parsed object and its serialized form.
  const NOTHING: ReadonlyArray<[string, ToolConversationPart["output"]]> = [
    ["an empty object", {}],
    ["a serialized empty object", "{}"],
    ["a serialized empty list", "[]"],
    ['the string "null"', "null"],
  ];
  for (const [label, output] of NOTHING) {
    it(`says the same for ${label}`, () => {
      const step = stepOf({ name: "mystery_tool", input: { table: "orders" }, output });
      expect(step.data).toBeUndefined();
      expect(bodyText(step)).toContain("This call returned nothing.");
    });
  }

  it("still counts a result that has fields", () => {
    const step = stepOf({ name: "mystery_tool", output: { rows: 7 } });
    expect(step.data).toEqual({ kind: "count", text: "1 field" });
  });
});

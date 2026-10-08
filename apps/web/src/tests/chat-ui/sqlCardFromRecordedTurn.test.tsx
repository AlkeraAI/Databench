// The SQL card, fed the turn a real machine recorded.
//
// `sqlResultProvenance.test.tsx` renders the card over a part typed by hand.
// This one folds the demo's own recorded Tinybird turn (the fixture the
// machine's promote tests read) through the real fold and the real step
// resolver, and asserts the five facts shown "without a click" come out
// of what the tool ACTUALLY emitted — the nested `provenance` block, the
// `call_tool` wrapper, the JSON-string output — not out of a shape this suite
// imagined for it.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";

import { renderBody, stepOf } from "./_steps";

const RECORDED = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../cli/tests/cloud/fixtures/recorded_sql_query_0f0bec47.json"),
    "utf8",
  ),
) as HarnessEvent[];

function recordedCard(): ToolConversationPart {
  const state = createConversationFoldState();
  for (const event of RECORDED) foldHarnessEvent(state, event);
  const [part] = state.turns
    .flatMap((turn) => turn.parts)
    .filter((candidate): candidate is ToolConversationPart => candidate.kind === "tool");
  return part;
}

describe("the SQL card over a recorded turn", () => {
  it("resolves to the SQL card, wrapper and all", () => {
    const step = stepOf(recordedCard());
    expect(step.expanded).toBe(true);
    const body = renderBody(step);
    expect(body.querySelector("[data-tool='sql_query']")).not.toBeNull();
  });

  it.each([
    ["the connection", "tinybird"],
    ["the engine", "· tinybird"],
    ["the role", "as tinybird"],
    ["the row count", "7 rows"],
    ["the duration", "156 ms"],
    ["the statement", "SELECT run_day AS day"],
  ])("shows %s without a click", (_case, text) => {
    const body = renderBody(stepOf(recordedCard()));
    expect(body.textContent).toContain(text);
  });

  it("shows the grid the machine returned", () => {
    const body = renderBody(stepOf(recordedCard()));
    const grid = body.querySelector("[role='table'][aria-label='Query result']");
    expect(grid).not.toBeNull();
    expect(grid?.textContent).toContain("2026-08-31");
    expect(grid?.textContent).toContain("1180");
  });
});

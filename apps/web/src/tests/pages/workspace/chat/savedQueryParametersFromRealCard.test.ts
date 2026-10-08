// Saving a query from the card a REAL turn produced, changing the customer
// parameter, and re-running produces a different, correct result.
//
// Other tests of the save-a-query path feed `querySpecOf` a statement written
// with `{customer}` slots (the compiler's own grammar). A recorded turn shows
// an agent writes the literal instead (`WHERE customer_id = 'cust_…'`), so the
// saved query must still offer a parameter the reader can change.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { unwrapCallTool, type ToolConversationPart } from "@alkera/chat-model";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { querySpecOf, sqlOf } from "@/pages/workspace/chat/saveResult";

const RECORDED = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../cli/tests/cloud/fixtures/recorded_sql_query_0f0bec47.json"),
    "utf8",
  ),
) as HarnessEvent[];

const CHAT_ID = "0f0bec47-69ea-417c-8850-fe1a22836979";

function savedCard(): ToolConversationPart {
  const state = createConversationFoldState();
  for (const event of RECORDED) foldHarnessEvent(state, event);
  const [part] = state.turns
    .flatMap((turn) => turn.parts)
    .filter((candidate): candidate is ToolConversationPart => candidate.kind === "tool");
  return unwrapCallTool(part);
}

describe("a query saved from the card a real turn produced", () => {
  it("the agent wrote its filter as a literal, not as a slot", () => {
    const sql = sqlOf(savedCard());
    expect(sql).toContain("WHERE");
    expect(sql).not.toMatch(/\{[a-z_][a-z0-9_]*\}/);
  });

  it("still offers a parameter the reader can change before re-running", () => {
    const spec = querySpecOf(savedCard(), CHAT_ID);
    expect(spec.params.length, "the saved query declares no parameter at all").toBeGreaterThan(0);
  });
});

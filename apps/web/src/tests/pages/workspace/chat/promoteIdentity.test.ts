// The browser half of the promote seam.
//
// The machine's half lives in `apps/cli/tests/cloud/test_cloud_promote_identity.py`
// and reads THIS SAME recording. Between them they pin one fact: the id the
// browser sends when a reader presses "Save as result" is the id the machine
// resolves — the tool CALL id, the only id both sides hold.
//
// It was not. The browser sent `prt_…` (the call) and the machine looked only
// for a finalized transcript event of that name, which an agent-run query never
// produces. Every promote of a real query was refused and its object sat in
// `pending_upload` for ever.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { unwrapCallTool, type ToolConversationPart } from "@alkera/chat-model";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { eventIdOf, previewOf, sqlOf } from "@/pages/workspace/chat/saveResult";

/** The recording the machine's own test reads, byte for byte. */
const RECORDED = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../cli/tests/cloud/fixtures/recorded_sql_query_0f0bec47.json"),
    "utf8",
  ),
) as HarnessEvent[];

const RECORDED_CALL_ID = "prt_07ad57d1c0016yL2P6MozuMrVf";

function toolParts(events: HarnessEvent[]): ToolConversationPart[] {
  const state = createConversationFoldState();
  for (const event of events) foldHarnessEvent(state, event);
  return state.turns
    .flatMap((turn) => turn.parts)
    .filter((part): part is ToolConversationPart => part.kind === "tool");
}

describe("what the browser names a tool result", () => {
  it("is the tool call id, on the card a reader would press Save on", () => {
    const [result] = toolParts(RECORDED);
    expect(result).toBeDefined();
    expect(eventIdOf(result)).toBe(RECORDED_CALL_ID);
  });

  it("is a name no transcript event in the recording carries", () => {
    // The premise of the machine-side fix: matching the event id could never
    // have resolved this promote.
    const eventIds = RECORDED.map((event) => String(event.event_id));
    expect(eventIds).not.toContain(RECORDED_CALL_ID);
    expect(eventIds.length).toBeGreaterThan(0);
  });

  it("names a card whose rows and SQL are the ones the machine uploads", () => {
    // What the reader was looking at when they pressed save has to be what gets
    // pinned; the machine's own test asserts the same seven rows and the same
    // statement come back out of the promote.
    // The card the reader presses Save on is the UNWRAPPED call — the same
    // unwrap the machine now performs before it reads the rows.
    const [result] = toolParts(RECORDED).map(unwrapCallTool);
    expect(result.name).toBe("sql.query");
    expect(eventIdOf(result)).toBe(RECORDED_CALL_ID);
    const preview = previewOf(result);
    expect(preview.columns).toEqual(["day", "shipments"]);
    expect(preview.rows).toHaveLength(7);
    expect(preview.rows[0]).toEqual(["2026-08-31", 1180]);
    expect(sqlOf(result)).toContain("SELECT run_day AS day");
  });
});

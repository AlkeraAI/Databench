// A question the chat has already answered gets answered out of the transcript:
// correct prose, a correct table, and NO new tool call — so there is no new card,
// and the earlier one is the only place the numbers are.
//
// The fold is where that is recoverable: a settled run whose step declares
// itself open stays open, so the card the earlier turn produced is still on
// screen. What this pins is that it survives the later prose turn rather than
// being buried behind a click a presenter has to know to make.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChatPanel } from "@alkera/ui";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { transcriptEntries, type EntryContext } from "@/pages/workspace/chat/entries";

const RECORDED = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../cli/tests/cloud/fixtures/recorded_sql_query_0f0bec47.json"),
    "utf8",
  ),
) as HarnessEvent[];

/** The turn the agent answers out of its own transcript: prose, no tool call. */
const FROM_MEMORY: HarnessEvent[] = [
  { event_type: "message.created", message_id: "u2", role: "user" },
  {
    event_type: "part.created",
    message_id: "u2",
    part: {
      part_id: "u2-t",
      message_id: "u2",
      type: "text",
      text: "and how many citations per day last week?",
    },
  },
  { event_type: "message.created", message_id: "a2", role: "assistant" },
  {
    event_type: "part.created",
    message_id: "a2",
    part: {
      part_id: "a2-t",
      message_id: "a2",
      type: "text",
      text: "I ran this earlier this session — same answer.",
    },
  },
  { event_type: "turn.completed", turn_id: "t2", event_id: "turn-2", stop_reason: "end_turn" },
];

function ctx(): EntryContext {
  return {
    onLinkClick: vi.fn(),
    onResourceOpen: vi.fn(),
    onOpenUrl: vi.fn(),
    onSubagentOpen: vi.fn(),
    onLineageOpen: vi.fn(),
    onKnowledgeOpen: vi.fn(),
    onPlanOpen: vi.fn(),
    questionLive: null,
  };
}

function renderTranscript(events: HarnessEvent[]) {
  const state = createConversationFoldState();
  for (const event of events) foldHarnessEvent(state, event);
  render(<ChatPanel entries={transcriptEntries(state.turns, ctx())} />);
}

describe("a question answered out of the transcript", () => {
  it("leaves the earlier result's card on screen, with no click", () => {
    renderTranscript([...RECORDED, ...FROM_MEMORY]);

    expect(screen.getByText("I ran this earlier this session — same answer.")).toBeInTheDocument();
    expect(document.querySelector('[data-tool="sql_query"]')).not.toBeNull();
  });

  // Saved queries and saved results were retired, so the card that is still on
  // screen offers nothing that would post to a route answering 410.
  it("offers nothing to save off that card", () => {
    renderTranscript([...RECORDED, ...FROM_MEMORY]);

    expect(screen.queryByRole("button", { name: /^Save as/ })).toBeNull();
  });
});

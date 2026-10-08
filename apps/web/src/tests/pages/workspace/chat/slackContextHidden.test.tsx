// A Slack-born turn: the person's bubble is their words, never the briefing.
//
// The box hands the model a briefing + the channel's history on the hidden
// per-turn channel, and the harness echoes that channel back as a leading
// SYNTHETIC text part on the user message. The transcript must show exactly
// the words the person typed in Slack ("hi"), on the user's side, and none of
// the context around them.

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChatPanel } from "@alkera/ui";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { transcriptEntries, type EntryContext } from "@/pages/workspace/chat/entries";

const BRIEFING =
  "You are answering inside a Slack thread, not the Alkera web app.\n" +
  "<slack-channel-history>\n<@U0AMY>: morning all\n</slack-channel-history>\n" +
  "<@U0AMY> asked:";

const ctx: EntryContext = {
  onLinkClick: vi.fn(),
  onResourceOpen: vi.fn(),
  onOpenUrl: vi.fn(),
  onSubagentOpen: vi.fn(),
  onLineageOpen: vi.fn(),
  onKnowledgeOpen: vi.fn(),
  onPlanOpen: vi.fn(),
  questionLive: null,
};

/** The harness's echo of a Slack-born first turn, as opencode publishes it: the
 *  hidden context leads the user message as a synthetic part, the words follow. */
function slackFirstTurn(): HarnessEvent[] {
  return [
    { event_type: "message.created", message_id: "u1", role: "user" },
    {
      event_type: "part.created",
      message_id: "u1",
      part: {
        part_id: "u1-context",
        message_id: "u1",
        type: "text",
        text: `<system-reminder>\n${BRIEFING}\n</system-reminder>`,
        synthetic: true,
      },
    },
    {
      event_type: "part.created",
      message_id: "u1",
      part: { part_id: "u1-words", message_id: "u1", type: "text", text: "hi" },
    },
    { event_type: "message.created", message_id: "a1", role: "assistant" },
    {
      event_type: "part.created",
      message_id: "a1",
      part: { part_id: "a1-text", message_id: "a1", type: "text", text: "Two connections." },
    },
  ];
}

describe("a Slack-born turn in the browser transcript", () => {
  it("folds the user turn to the words alone", () => {
    const state = createConversationFoldState({ agentHost: "workspace" });
    for (const event of slackFirstTurn()) foldHarnessEvent(state, event);

    const user = state.turns.find((turn) => turn.author === "user");
    expect(user).toBeDefined();
    expect(user?.parts).toEqual([
      { id: "u1-words", kind: "text", text: "hi", streaming: false },
    ]);
  });

  it("renders only the words on the user's side", () => {
    const state = createConversationFoldState({ agentHost: "workspace" });
    for (const event of slackFirstTurn()) foldHarnessEvent(state, event);
    render(<ChatPanel entries={transcriptEntries(state.turns, ctx)} />);

    expect(screen.getByText("hi")).toBeInTheDocument();
    expect(screen.getByText("Two connections.")).toBeInTheDocument();
    expect(screen.queryByText(/answering inside a Slack thread/)).toBeNull();
    expect(screen.queryByText(/slack-channel-history/)).toBeNull();
    expect(screen.queryByText(/asked:/)).toBeNull();
  });

  it("keeps the words even when the context part streams in first", () => {
    // The live shape: opencode starts the synthetic part, then the words.
    const state = createConversationFoldState({ agentHost: "workspace" });
    const events: HarnessEvent[] = [
      { event_type: "message.created", message_id: "u1", role: "user" },
      {
        event_type: "part.started",
        message_id: "u1",
        part_id: "u1-context",
        part_type: "text",
        initial: { type: "text", text: "<system-reminder>", synthetic: true },
      },
      {
        event_type: "part.started",
        message_id: "u1",
        part_id: "u1-words",
        part_type: "text",
        initial: { type: "text", text: "hi" },
      },
    ];
    for (const event of events) foldHarnessEvent(state, event);

    const user = state.turns.find((turn) => turn.author === "user");
    expect(user?.parts.map((part) => (part.kind === "text" ? part.text : part.kind))).toEqual([
      "hi",
    ]);
  });
});

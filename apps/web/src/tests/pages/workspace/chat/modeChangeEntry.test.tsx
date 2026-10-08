// A permission-mode change is a card in the transcript: the new mode leading,
// old → new, and who changed it from where. The server writes it whichever
// surface the change came from, and the words are the shared presentation the
// Slack thread's card is built from too.
import { describe, expect, it } from "vitest";

import {
  cloneTurns,
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { transcriptEntries } from "@/pages/workspace/chat/entries";
import { stripPermissionParts } from "@/pages/workspace/chat/stripPermissionParts";

const NOOP = () => {};
const CTX = {
  onOpenUrl: NOOP,
  onLinkClick: NOOP,
  onResourceOpen: NOOP,
  onSubagentOpen: NOOP,
  onPlanOpen: NOOP,
  onLineageOpen: NOOP,
  onKnowledgeOpen: NOOP,
  questionLive: null,
};

function entriesOf(events: HarnessEvent[]) {
  const state = createConversationFoldState();
  for (const event of events) foldHarnessEvent(state, event);
  return transcriptEntries(stripPermissionParts(cloneTurns(state)), CTX);
}

const CHANGE: HarnessEvent = {
  event_type: "mode.changed",
  event_id: "aside-mode-1",
  mode: "bypass",
  previous_mode: "default",
  decided_by_name: "Robin Lee",
  decided_via: "slack",
};

describe("a mode change", () => {
  it("is a card naming the new mode, the old one, who and where", () => {
    const shown = entriesOf([CHANGE]);
    expect(shown).toHaveLength(1);
    expect(shown[0]?.item).toEqual({
      kind: "notice",
      level: "neutral",
      title: "Mode set to Bypass permissions",
      body: "Default → Bypass permissions · Changed in Slack by Robin Lee",
      action: null,
    });
  });

  it("made on the web says so", () => {
    const shown = entriesOf([{ ...CHANGE, decided_via: "web", mode: "plan", previous_mode: "auto" }]);
    expect(shown[0]?.item).toMatchObject({
      title: "Mode set to Plan",
      body: "Auto → Plan · Changed on web by Robin Lee",
    });
  });

  it("with no mode named is nothing", () => {
    expect(entriesOf([{ ...CHANGE, mode: "" }])).toEqual([]);
  });
});

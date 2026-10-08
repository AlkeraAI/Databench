// An ask a person was shown settles, in the transcript, to one line naming the
// outcome, who decided and where — the shared outcome line a Slack card
// settles to as well, so an ask answered in Slack reads "… in Slack by …" here
// and one answered here reads "… on web by …" there. A pending ask is still an
// interrupt (the dock), never a transcript entry; an ask the policy settled
// before anybody was asked leaves no entry at all.
import { describe, expect, it } from "vitest";

import type { PermissionConversationPart } from "@alkera/chat-model";
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

const ASK: HarnessEvent = {
  event_type: "permission.request",
  message_id: "msg-1",
  request_id: "per_1",
  permission_kind: "bash",
  patterns: ["uv run python -c 'print(1)'"],
  prompting: true,
  options: [
    { option_id: "allow_once", name: "Allow once" },
    { option_id: "reject_once", name: "Reject once" },
    { option_id: "deny_always", name: "Never allow" },
  ],
};

function fold(events: HarnessEvent[]) {
  const state = createConversationFoldState();
  for (const event of [
    { event_type: "message.created", message_id: "msg-1", role: "assistant" },
    ...events,
  ]) {
    foldHarnessEvent(state, event);
  }
  return cloneTurns(state);
}

const entries = (turns: ReturnType<typeof fold>) =>
  transcriptEntries(stripPermissionParts(turns), CTX);

const askPart = (turns: ReturnType<typeof fold>): PermissionConversationPart => {
  const part = turns
    .flatMap((turn) => turn.parts)
    .find((candidate): candidate is PermissionConversationPart => candidate.kind === "permission");
  if (!part) throw new Error("no permission part folded");
  return part;
};

describe("a decided ask", () => {
  it.each([
    [
      "answered in Slack by a named member",
      {
        option_id: "allow_once",
        decided_by: "user",
        decided_by_user_id: "u-1",
        decided_by_name: "Dana Okafor",
        decided_via: "slack",
      },
      "Allowed in Slack by Dana Okafor",
    ],
    [
      "refused on the web",
      { option_id: "reject_once", decided_by: "user", decided_by_name: "Sam Lee", decided_via: "web" },
      "Denied on web by Sam Lee",
    ],
    [
      "re-decided by a mode switched while it waited",
      { option_id: "allow_once", decided_by: "policy" },
      "Decided by the new mode",
    ],
    [
      "allowed by a member the event does not name",
      { option_id: "allow_once", decided_by: "user" },
      "Allowed by a member of this workspace",
    ],
  ])("settles to one line when %s", (_how, resolution, line) => {
    const turns = fold([ASK, { event_type: "permission.resolved", request_id: "per_1", ...resolution }]);
    const shown = entries(turns);
    expect(shown).toHaveLength(1);
    expect(shown[0]?.status).toBe("settled");
    expect(shown[0]?.item).toMatchObject({ kind: "notice", level: "neutral", title: line });
  });

  it("keeps the first telling's who and where when the box's copy lands after", () => {
    const turns = fold([
      ASK,
      {
        event_type: "permission.resolved",
        request_id: "per_1",
        option_id: "allow_once",
        decided_by: "user",
        decided_by_name: "Dana Okafor",
        decided_via: "web",
      },
      { event_type: "permission.resolved", request_id: "per_1", option_id: "allow_once", decided_by: "user" },
    ]);
    expect(entries(turns)[0]?.item).toMatchObject({ title: "Allowed on web by Dana Okafor" });
  });

  it("settles the same way when the resolution lands before the ask", () => {
    const turns = fold([
      {
        event_type: "permission.resolved",
        request_id: "per_1",
        option_id: "allow_once",
        decided_by: "user",
        decided_by_name: "Dana Okafor",
        decided_via: "slack",
      },
      ASK,
    ]);
    expect(entries(turns)[0]?.item).toMatchObject({ title: "Allowed in Slack by Dana Okafor" });
  });

  it("leaves no entry for an ask the policy settled before anybody was asked", () => {
    const unseen = { ...ASK, prompting: false };
    const turns = fold([
      unseen,
      { event_type: "permission.resolved", request_id: "per_1", option_id: "allow_once", decided_by: "policy" },
    ]);
    expect(entries(turns)).toEqual([]);
  });

  it("keeps the decision on the part, for the dock and the register", () => {
    const turns = fold([
      ASK,
      {
        event_type: "permission.resolved",
        request_id: "per_1",
        option_id: "allow_once",
        decided_by: "user",
        decided_by_user_id: "u-1",
        decided_by_name: "Dana Okafor",
      },
    ]);
    const part = askPart(turns);
    expect(part.status).toBe("resolved");
    expect(part.selectedOptionId).toBe("allow_once");
    expect(part.decidedByName).toBe("Dana Okafor");
  });

  it("while pending, it is an interrupt and not an entry", () => {
    expect(entries(fold([ASK]))).toEqual([]);
    expect(askPart(fold([ASK])).status).toBe("pending");
  });
});

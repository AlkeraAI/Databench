// Two Stops one turn apart: the first cuts a running tool, the second cuts
// prose mid-word, and the second's three rows land between the text part's
// `part.started` and its `part.created`. Both notices read the same sentence,
// word for word, under different ids. Folded, mapped and drawn exactly as the
// chat page does it, both keep their text — at every point of the second turn
// a reader could be looking at, and however the tab came by the rows.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChatPanel } from "@alkera/ui";

import {
  liveView,
  referenceView,
  snapshotView,
  type FoldView,
  type LogRow,
} from "@/pages/workspace/chat/data/convergence";
import { transcriptEntries } from "@/pages/workspace/chat/entries";
import { stripPermissionParts } from "@/pages/workspace/chat/stripPermissionParts";

import late from "@/tests/fixtures/chat-convergence/52f6f441-d4f3-4cf6-a3eb-8b58b6f21fc8/log.json";

const LOG = (late as { rows: LogRow[] }).rows;
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
const FIRST = "stop-82a7c5e3eff8";
const SECOND = "stop-988da9a7837f";

function noticesDrawn(view: FoldView): string[] {
  const entries = transcriptEntries(stripPermissionParts(view.turns), CTX);
  const { container, unmount } = render(<ChatPanel entries={entries} />);
  const drawn = [...container.querySelectorAll("[data-block='notice']")].map((el) => el.textContent ?? "");
  unmount();
  return drawn;
}

const noteOf = (view: FoldView, id: string) =>
  view.turns.find((turn) => turn.id === id)?.parts.map((part) => ("text" in part ? part.text : part.kind));

describe("two Stop notices one turn apart", () => {
  it("the server gives each Stop its own message and part ids", () => {
    const ids = LOG.filter((row) => /^stop-/.test(row.event_id ?? "")).map((row) => row.event_id);
    expect(new Set(ids).size).toBe(6);
    expect(ids).toEqual([
      `${FIRST}-created`,
      `${FIRST}-text`,
      `${FIRST}-done`,
      `${SECOND}-created`,
      `${SECOND}-text`,
      `${SECOND}-done`,
    ]);
  });

  it.each([
    [26, 1, "the first turn over"],
    [41, 1, "the second turn streaming, before its Stop"],
    [44, 2, "the second Stop recorded, the cut prose not yet settled"],
    [45, 2, "the cut prose settled"],
  ])("at %i every notice drawn has its sentence (%i) — %s", async (n, count) => {
    const reference = await referenceView(LOG, n);
    const drawn = noticesDrawn(reference);
    expect(drawn).toHaveLength(count);
    for (const text of drawn) expect(text).toBe("[scrubbed 28]");
    expect(noteOf(reference, FIRST)).toEqual(["[scrubbed 28]"]);
  });

  it.each([
    ["a tab that watched both turns", () => liveView(LOG, 1, 45)],
    ["a tab that opened between the two Stops", () => liveView(LOG, 30, 45)],
    ["a reload, socket first", () => snapshotView(LOG, 45, { order: "socket-first" as const })],
    ["a reload, page first", () => snapshotView(LOG, 45, { order: "rest-first" as const })],
  ])("the second Stop never takes the first one's text — %s", async (_how, open) => {
    const view = await open();
    expect(noteOf(view, FIRST)).toEqual(["[scrubbed 28]"]);
    expect(noteOf(view, SECOND)).toEqual(["[scrubbed 28]"]);
    expect(noticesDrawn(view)).toEqual(["[scrubbed 28]", "[scrubbed 28]"]);
  });
});

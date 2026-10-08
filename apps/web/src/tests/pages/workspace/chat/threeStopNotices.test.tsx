// Three Stops: one that cut a running tool, then two that cut prose, the last
// after ~3,000 streamed characters. A browser probe read the FIRST notice —
// the one directly under the "Ran 1 terminal command / 1 failed" group — as
// empty once the third turn had pushed it off screen, live and after a reload.
//
// The sentence never left the page. The tape's blocks are `content-visibility:
// auto` (panel.css), so a block far enough off screen skips layout, and
// `innerText` — which is computed from layout — reads "" for it while the
// `<p>` holding the words is still in the document (`textContent`, find-in-
// page and a scroll back all have it). This pins the part that is the
// product's: from the recorded rows, every Stop's notice is an entry with its
// sentence, keyed by its own part id, drawn with its title — at every point
// of the three turns, next to the tool group or not.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChatPanel } from "@alkera/ui";

import { liveView, referenceView, snapshotView, type FoldView, type LogRow } from "@/pages/workspace/chat/data/convergence";
import { transcriptEntries } from "@/pages/workspace/chat/entries";
import { stripPermissionParts } from "@/pages/workspace/chat/stripPermissionParts";

import three from "@/tests/fixtures/chat-convergence/ada0e7f2-3ecf-4f98-911a-21caf94591fa/log.json";

const LOG = (three as { rows: LogRow[] }).rows;
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
const STOPS = ["stop-7ce48fff7cec", "stop-41ed9a970880", "stop-894cb670a386"];
const SENTENCE = "[scrubbed 28]";

/** The notices as the panel draws them: the entry id each block carries, the
 *  words in its title element, and the block drawn just above it. */
function drawn(view: FoldView): Array<{ id: string | null; title: string; above: string | null }> {
  const entries = transcriptEntries(stripPermissionParts(view.turns), CTX);
  const { container, unmount } = render(<ChatPanel entries={entries} />);
  const out = [...container.querySelectorAll("[data-block='notice']")].map((block) => ({
    id: block.getAttribute("data-entry-id"),
    title: block.querySelector("p")?.textContent ?? "",
    above: block.previousElementSibling?.getAttribute("data-block") ?? null,
  }));
  unmount();
  return out;
}

describe("three Stop notices, the first under a failed tool group", () => {
  it.each([
    [26, 1, "the tool turn over"],
    [47, 2, "the second turn over"],
    [63, 3, "the third Stop recorded, 3,000 characters of prose not yet settled"],
    [68, 3, "the whole log"],
  ])("at %i each of the %i notices is drawn with its sentence — %s", async (n, count) => {
    const notices = drawn(await referenceView(LOG, n));
    expect(notices.map((notice) => notice.id)).toEqual(STOPS.slice(0, count).map((id) => `${id}-part`));
    for (const notice of notices) expect(notice.title).toBe(SENTENCE);
    // The one the probe read as empty: directly under the tool group it cut.
    expect(notices[0].above).toBe("activity");
  });

  it.each([
    ["a tab that watched all three turns", () => liveView(LOG, 1, 68)],
    ["a tab that opened after the first Stop", () => liveView(LOG, 27, 68)],
    ["a reload, socket first", () => snapshotView(LOG, 68, { order: "socket-first" as const })],
    ["a reload, page first", () => snapshotView(LOG, 68, { order: "rest-first" as const })],
  ])("no later turn takes an earlier notice's words — %s", async (_how, open) => {
    const view = await open();
    for (const id of STOPS) {
      const turn = view.turns.find((candidate) => candidate.id === id);
      expect(turn?.parts.map((part) => ("text" in part ? part.text : part.kind))).toEqual([SENTENCE]);
    }
    expect(drawn(view).map((notice) => notice.title)).toEqual([SENTENCE, SENTENCE, SENTENCE]);
  });

  it("the tape's blocks skip layout off screen, which is what an innerText probe reads as empty", () => {
    // Not a behaviour to change: it is why a long chat costs what is in view.
    // Pinned so the next reader of an "empty block" report looks here first.
    const css = readFileSync(resolve(__dirname, "../../../../../../../packages/ui/src/chat/panel/panel.css"), "utf8");
    const rule = /\.chat-root \.chat-tape > \.chat-block \{[^}]*\}/.exec(css)?.[0] ?? "";
    expect(rule).toContain("content-visibility: auto");
  });
});

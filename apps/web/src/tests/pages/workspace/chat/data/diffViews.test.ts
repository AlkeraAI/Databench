// The comparison the convergence net and the browser detector both stand on.
// It walked only the SHORTER view's turns, so a turn the longer view alone
// held was never reported — which is exactly the shape of a Stop note missing
// from the tab that watched the Stop. Both directions are reported, always.
// The one thing excused is what a view CLAIMS not to have loaded: a tab opened
// on a page says there is more above it (`hasOlder`), and turns above the first
// one the two views share are then history, not a drop. Nothing else — no
// position in the view, no missing neighbour, no view of one turn or none —
// switches a direction off.

import { describe, expect, it } from "vitest";

import type { ConversationTurn } from "@alkera/chat-model";

import { diffViews, type FoldView } from "@/pages/workspace/chat/data/convergence";

const turn = (id: string, author: ConversationTurn["author"] = "assistant"): ConversationTurn => ({
  id,
  author,
  status: "done",
  parts: [{ id: `${id}-p`, kind: "text", text: id }],
});

const view = (turns: ConversationTurn[], hasOlder = false): FoldView => ({
  turns,
  pendingAsk: null,
  awaitsResponse: false,
  turnState: "idle",
  hasOlder,
});

const WHOLE = [
  turn("stop-0", "system"),
  turn("u1", "user"),
  turn("stop-1", "system"),
  turn("a1"),
  turn("u2", "user"),
  turn("a2"),
  turn("stop-2", "system"),
];
const without = (...ids: string[]) => WHOLE.filter((t) => !ids.includes(t.id));
const paths = (live: FoldView, snapshot: FoldView) => diffViews(live, snapshot).map((d) => d.path).sort();

describe("diffViews reports a turn either side alone holds", () => {
  it("a Stop note that is first in the log, missing from live", () => {
    expect(paths(view(without("stop-0")), view(WHOLE))).toEqual(["turn[0:stop-0] missing from live"]);
  });

  it("a Stop note between the shorter view's first and second turns", () => {
    expect(paths(view(without("stop-0", "stop-1")), view(WHOLE))).toEqual([
      "turn[0:stop-0] missing from live",
      "turn[2:stop-1] missing from live",
    ]);
  });

  it("every Stop note at once, and from either side", () => {
    expect(paths(view(without("stop-0", "stop-1", "stop-2")), view(WHOLE))).toEqual([
      "turn[0:stop-0] missing from live",
      "turn[2:stop-1] missing from live",
      "turn[6:stop-2] missing from live",
    ]);
    expect(paths(view(WHOLE), view(without("stop-1")))).toEqual(["turn[2:stop-1] missing from snapshot"]);
  });

  it("a shorter view of no turns at all", () => {
    expect(paths(view([]), view([turn("stop-0", "system"), turn("u1", "user")]))).toEqual([
      "turn[0:stop-0] missing from live",
      "turn[1:u1] missing from live",
    ]);
  });

  it("a shorter view of one turn", () => {
    expect(paths(view([turn("u1", "user")]), view(WHOLE.slice(0, 3)))).toEqual([
      "turn[0:stop-0] missing from live",
      "turn[2:stop-1] missing from live",
    ]);
  });

  it("a shorter view whose second turn the longer one does not hold", () => {
    // The page invented an `assistant-…` turn for a message above it: that turn
    // is its own finding, and it must not blind the comparison to the rest.
    const live = [turn("u1", "user"), turn("assistant-invented"), turn("u2", "user"), turn("a2")];
    expect(paths(view(live), view(WHOLE))).toEqual([
      "turn[0:stop-0] missing from live",
      "turn[1:assistant-invented] missing from snapshot",
      "turn[2:stop-1] missing from live",
      "turn[3:a1] missing from live",
      "turn[6:stop-2] missing from live",
    ]);
  });

  it("two views that share no turn report every turn of both", () => {
    expect(paths(view([turn("x")]), view([turn("y"), turn("z")]))).toEqual([
      "turn[0:x] missing from snapshot",
      "turn[0:y] missing from live",
      "turn[1:z] missing from live",
    ]);
  });

  it("says which side holds the turn", () => {
    const [missing] = diffViews(view(without("stop-2")), view(WHOLE));
    expect(missing).toMatchObject({ live: undefined, snapshot: "system" });
    const [extra] = diffViews(view(WHOLE), view(without("stop-2")));
    expect(extra).toMatchObject({ live: "system", snapshot: undefined });
  });

  it("the same number of turns, a different one on each side", () => {
    expect(paths(view(without("stop-1")), view(without("stop-2")))).toEqual([
      "turn[2:stop-1] missing from live",
      "turn[5:stop-2] missing from snapshot",
    ]);
  });

  describe("a tab that says it has older turns to load", () => {
    const tail = WHOLE.slice(4);

    it("is not missing the history above the first turn the two share", () => {
      expect(paths(view(tail, true), view(WHOLE))).toEqual([]);
      expect(paths(view(WHOLE), view(tail, true))).toEqual([]);
    });

    it("is still missing a turn inside the tail it holds", () => {
      const holed = tail.filter((t) => t.id !== "stop-2");
      expect(paths(view(holed, true), view(WHOLE))).toEqual(["turn[6:stop-2] missing from live"]);
    });

    it("a turn the two views place differently does not drag its floor up", () => {
      // 3f0f77f4 at 829: a message said mid-turn stands where it was SAID in the
      // tab that heard it, and where the box ECHOED it on a page. The page
      // starts at its own first turn, not at the lowest place a shared turn has.
      const said = turn("said-mid-turn", "user");
      const watched = [turn("old-1"), said, turn("old-2"), turn("old-3"), turn("t1"), turn("t2")];
      const page = [turn("t1"), said, turn("t2")];
      expect(paths(view(page, true), view(watched))).toEqual([]);
      expect(paths(view(watched), view(page, true))).toEqual([]);
    });

    it("starts at the first of its turns the other view holds, when its own first is not one", () => {
      const watched = [turn("old-1"), turn("t1"), turn("dropped"), turn("t2")];
      const page = [turn("assistant-invented"), turn("t1"), turn("t2")];
      expect(paths(view(page, true), view(watched))).toEqual([
        "turn[0:assistant-invented] missing from snapshot",
        "turn[2:dropped] missing from live",
      ]);
    });

    it("and the same tail from a tab that claims nothing is missing all of it", () => {
      expect(paths(view(tail), view(WHOLE))).toEqual([
        "turn[0:stop-0] missing from live",
        "turn[1:u1] missing from live",
        "turn[2:stop-1] missing from live",
        "turn[3:a1] missing from live",
      ]);
    });
  });

  it("two views that agree report nothing", () => {
    expect(diffViews(view(WHOLE), view(WHOLE))).toEqual([]);
  });
});

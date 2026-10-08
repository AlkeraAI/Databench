import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { accountKey } from "@alkera/ui/storage";

import {
  createLayoutWriter,
  PANE_BOUNDS,
  PANES_KEPT,
  RAIL_BOUNDS,
  readLayout,
  writeLayout,
  type ChatLayout,
} from "@/pages/workspace/chat/workspace/layoutStorage";

// How wide the two side panes are, and whether either is folded away, is a fact
// about this laptop rather than about the chat — so it lives in `localStorage`
// and nowhere else. Everything pinned here is a refusal: a store that throws, a
// value another build wrote, a width that would leave a pane unusable. None of
// them may reach the page; all of them read as "this browser has not been told
// yet", which is the only reading a split view can lay out from.

const USER = { userId: "usr_dana", orgId: "org_a" };
const SAM = { userId: "usr_sam", orgId: "org_a" };
const KEY = accountKey(USER.userId, USER.orgId, "chat.layout");

function stored(): unknown {
  return JSON.parse(localStorage.getItem(KEY) ?? "null");
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("the layout this browser remembers", () => {
  it("hands back what it was told, per account", () => {
    const mine: ChatLayout = {
      v: 1,
      rail: { collapsed: true, width: 200 },
      pane: { collapsed: false, width: 640 },
    };
    writeLayout(USER, mine);
    writeLayout(SAM, {
      v: 1,
      rail: { collapsed: false, width: 300 },
      pane: { collapsed: true, width: 400 },
    });

    expect(readLayout(USER)).toEqual(mine);
    // The other account's answer is the other account's, not the last write.
    expect(readLayout(SAM).rail).toEqual({ collapsed: false, width: 300 });
  });

  it("keeps one person's layout apart per org", () => {
    const inA: ChatLayout = {
      v: 1,
      rail: { collapsed: true, width: 200 },
      pane: { collapsed: false, width: 640 },
    };
    writeLayout(USER, inA);
    const inB = { userId: USER.userId, orgId: "org_b" };

    // The same person in another org has been told nothing in this browser.
    expect(readLayout(inB).rail).toEqual({ collapsed: false, width: RAIL_BOUNDS.size });
    expect(readLayout(inB).pane.width).toBe(PANE_BOUNDS.size);
    writeLayout(inB, { v: 1, rail: { collapsed: false, width: 320 }, pane: { collapsed: true, width: 500 } });
    // And arranging the panes there leaves org A's arrangement as it was.
    expect(readLayout(USER)).toEqual(inA);
  });

  it("reads a value it cannot parse as no value at all", () => {
    localStorage.setItem(KEY, "{not json");

    expect(readLayout(USER)).toEqual({
      v: 1,
      rail: { collapsed: false, width: RAIL_BOUNDS.size },
      pane: { collapsed: false, width: PANE_BOUNDS.size },
    });
  });

  it("ignores a document written under a different shape", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ v: 2, rail: { collapsed: true, width: 200 }, pane: { collapsed: true, width: 400 } }),
    );

    expect(readLayout(USER).rail).toEqual({ collapsed: false, width: RAIL_BOUNDS.size });
  });

  it("ignores fields of the wrong type rather than laying out from them", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ v: 1, rail: { collapsed: "yes", width: "wide" }, pane: { collapsed: 1, width: null } }),
    );

    expect(readLayout(USER)).toEqual({
      v: 1,
      rail: { collapsed: false, width: RAIL_BOUNDS.size },
      pane: { collapsed: false, width: PANE_BOUNDS.size },
    });
  });

  it("clamps a restored width into the pane's own range", () => {
    writeLayout(USER, {
      v: 1,
      rail: { collapsed: false, width: 9_000 },
      pane: { collapsed: false, width: 4 },
    });

    const restored = readLayout(USER);
    expect(restored.rail.width).toBe(RAIL_BOUNDS.max);
    expect(restored.pane.width).toBe(PANE_BOUNDS.min);
  });

  it("clamps against the caller's own bounds when the viewport narrows them", () => {
    writeLayout(USER, {
      v: 1,
      rail: { collapsed: false, width: 460 },
      pane: { collapsed: false, width: 900 },
    });

    // 60% of a 1000px viewport is the widest the workspace pane may be here.
    const restored = readLayout(USER, { pane: { max: 600 } });
    expect(restored.pane.width).toBe(600);
    // A bound the caller did not narrow is untouched.
    expect(restored.rail.width).toBe(460);
  });

  it("opens the workspace pane or folds it away when nothing was stored", () => {
    expect(readLayout(USER, { paneCollapsedByDefault: true }).pane.collapsed).toBe(true);
    expect(readLayout(USER, { paneCollapsedByDefault: false }).pane.collapsed).toBe(false);
  });

  it("prefers what was stored over the caller's first-visit default", () => {
    writeLayout(USER, {
      v: 1,
      rail: { collapsed: false, width: RAIL_BOUNDS.size },
      pane: { collapsed: false, width: PANE_BOUNDS.size },
    });

    expect(readLayout(USER, { paneCollapsedByDefault: true }).pane.collapsed).toBe(false);
  });

  it("survives a store that throws on every access", () => {
    const hostile = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
      key: () => null,
      clear: () => undefined,
      length: 0,
    };
    vi.stubGlobal("localStorage", hostile);

    expect(readLayout(USER).pane.width).toBe(PANE_BOUNDS.size);
    expect(() =>
      writeLayout(USER, { v: 1, rail: { collapsed: true, width: 200 }, pane: { collapsed: true, width: 400 } }),
    ).not.toThrow();
  });

  it("remembers nothing for a reader with no account", () => {
    writeLayout(null, { v: 1, rail: { collapsed: true, width: 200 }, pane: { collapsed: true, width: 400 } });

    expect(localStorage.length).toBe(0);
    expect(readLayout(undefined).rail.collapsed).toBe(false);
  });

  it("stores the width it will hand back, not the one it was given", () => {
    writeLayout(USER, { v: 1, rail: { collapsed: false, width: 12 }, pane: { collapsed: false, width: 640 } });

    expect(stored()).toEqual({
      v: 1,
      rail: { collapsed: false, width: RAIL_BOUNDS.min },
      pane: { collapsed: false, width: 640 },
    });
  });
});

describe("the width a gesture settles on", () => {
  const layout = (width: number): ChatLayout => ({
    v: 1,
    rail: { collapsed: false, width: 256 },
    pane: { collapsed: false, width },
  });

  it("keeps the last width of a gesture and none of the ones it crossed", async () => {
    const writer = createLayoutWriter(5);

    for (const width of [576, 566, 556, 546]) writer.remember(USER, layout(width));
    // The gesture is still in flight: nothing it has passed over is a
    // preference, so this browser has not been told anything yet.
    expect(stored()).toBeNull();

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(readLayout(USER).pane.width).toBe(546);
  });

  it("writes the width owed when the page goes away mid-gesture", () => {
    const writer = createLayoutWriter(5_000);
    writer.remember(USER, layout(500));

    writer.flush();

    // A width chosen a moment before leaving is still a width the reader chose:
    // it comes back on the next visit rather than being dropped with the timer.
    expect(readLayout(USER).pane.width).toBe(500);
  });

  it("has nothing to write when the reader moved nothing", () => {
    const writer = createLayoutWriter(5);

    writer.flush();

    expect(stored()).toBeNull();
  });
});

describe("the pane is the chat's, the rail is the account's", () => {
  // One conversation is a column of prose and the next is three files side by
  // side. A reader who widened the pane for the second did not ask for the
  // first to open that way, so the pane is the chat's — while the rail, which
  // lists every chat, stays one answer for the account.
  const layout = (pane: number, rail = 256): ChatLayout => ({
    v: 1,
    rail: { collapsed: false, width: rail },
    pane: { collapsed: false, width: pane },
  });
  const paneKey = (user: string, chat: string): string => `alkera.size:chat.pane:${user}:${chat}`;

  it("gives each chat back the width it was left at", () => {
    writeLayout(USER, layout(640), { chatId: "c1" });
    writeLayout(USER, layout(380), { chatId: "c2" });

    expect(readLayout(USER, { chatId: "c1" }).pane.width).toBe(640);
    expect(readLayout(USER, { chatId: "c2" }).pane.width).toBe(380);
  });

  it("keeps the rail one width across every chat", () => {
    writeLayout(USER, layout(640, 200), { chatId: "c1" });
    writeLayout(USER, layout(380, 300), { chatId: "c2" });

    expect(readLayout(USER, { chatId: "c1" }).rail.width).toBe(300);
    expect(readLayout(USER, { chatId: "c2" }).rail.width).toBe(300);
  });

  it("opens a chat it has never laid out at the last width the reader chose", () => {
    writeLayout(USER, layout(700), { chatId: "c1" });

    // Not the default: the running default the account's own document carries.
    expect(readLayout(USER, { chatId: "c-new" }).pane.width).toBe(700);
    expect(readLayout(USER, { chatId: "c-new" }).pane.width).not.toBe(PANE_BOUNDS.size);
  });

  it("remembers a folded pane per chat, not just a width", () => {
    writeLayout(
      USER,
      { v: 1, rail: { collapsed: false, width: 256 }, pane: { collapsed: true, width: 576 } },
      { chatId: "c1" },
    );
    writeLayout(USER, layout(576), { chatId: "c2" });

    expect(readLayout(USER, { chatId: "c1" }).pane.collapsed).toBe(true);
    expect(readLayout(USER, { chatId: "c2" }).pane.collapsed).toBe(false);
  });

  it("does not let one account's chats answer for another's", () => {
    writeLayout(USER, layout(640), { chatId: "c1" });
    writeLayout(SAM, layout(400), { chatId: "c1" });

    expect(readLayout(USER, { chatId: "c1" }).pane.width).toBe(640);
    expect(readLayout(SAM, { chatId: "c1" }).pane.width).toBe(400);
  });

  it("clamps a chat's stored width to what this window can draw", () => {
    writeLayout(USER, layout(900), { chatId: "c1" });

    expect(readLayout(USER, { chatId: "c1", pane: { max: 480 } }).pane.width).toBe(480);
    // The reader's own choice survives the narrow window.
    expect(readLayout(USER, { chatId: "c1" }).pane.width).toBe(900);
  });

  it("keeps a bounded number of chats, oldest first", () => {
    const ids = Array.from({ length: PANES_KEPT + 3 }, (_, i) => `c${i}`);
    for (const id of ids) writeLayout(USER, layout(600), { chatId: id });

    const index = JSON.parse(
      localStorage.getItem(`alkera.size.lru:chat.pane:${USER.userId}`) ?? "[]",
    ) as string[];
    expect(index).toHaveLength(PANES_KEPT);
    // The three oldest chats fell off, entries and all.
    for (const id of ids.slice(0, 3)) expect(localStorage.getItem(paneKey(USER.userId, id))).toBeNull();
    expect(localStorage.getItem(paneKey(USER.userId, ids[ids.length - 1]!))).not.toBeNull();
  });

  it("writes nothing per chat when there is no chat yet", () => {
    writeLayout(USER, layout(640));

    expect(localStorage.getItem(`alkera.size.lru:chat.pane:${USER.userId}`)).toBeNull();
    // …and the account's running default still took the width.
    expect(readLayout(USER).pane.width).toBe(640);
  });
});

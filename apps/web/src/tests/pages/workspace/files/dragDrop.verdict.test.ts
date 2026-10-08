import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import {
  isFilesDrag,
  isInsideSubtree,
  isKnownDrag,
  isMoveDrag,
  leftElement,
  moveVerdict,
  POOL_HOLD_MS,
  runPool,
} from "@/pages/workspace/files/dragDrop";
import { MOVE_MIME, type DropTarget } from "@/pages/workspace/files/dropHandlers";

// The decisions a drag rests on, with nothing but data: what kind of drag is in
// the air, where a row may land, and how a batch is paced.

function item(over: Partial<Item> & { id: string; name: string }): Item {
  return {
    ino: 1,
    driveId: "dr_1",
    kind: "file",
    nameDisplay: over.name,
    parentId: "nd_home",
    path: `/home/${over.name}`,
    etag: "et",
    capabilities: { can_write: true },
    ...over,
  } as unknown as Item;
}

const HOME: DropTarget & { path: string } = {
  id: "nd_home",
  name: "home",
  kind: "folder",
  capabilities: { can_write: true },
  path: "/home",
};
const DEST = { ...HOME, id: "nd_dest", name: "dest", path: "/home/dest" };
const LOCKED = { ...DEST, id: "nd_locked", name: "locked", capabilities: { can_write: false } };
const SOURCE = { name: "home", canWrite: true };

const MOVER = item({ id: "nd_mover", name: "mover", kind: "folder" });
const REPORT = item({ id: "nd_report", name: "report.csv" });

describe("moveVerdict", () => {
  it("sends every row that is not already there", () => {
    expect(moveVerdict([MOVER, REPORT], DEST, SOURCE)).toEqual({
      moves: [MOVER, REPORT],
      unchanged: 0,
      refusal: null,
    });
  });

  it("does nothing for a row dropped onto the folder it is already in", () => {
    expect(moveVerdict([MOVER, REPORT], HOME, SOURCE)).toEqual({
      moves: [],
      unchanged: 2,
      refusal: null,
    });
  });

  it("sends only the rows that change folder when some are already there", () => {
    const elsewhere = item({ id: "nd_x", name: "x.csv", parentId: "nd_dest" });
    expect(moveVerdict([elsewhere, REPORT], DEST, SOURCE)).toEqual({
      moves: [REPORT],
      unchanged: 1,
      refusal: null,
    });
  });

  it.each([
    ["a file", { ...DEST, kind: "file" as const, name: "notes.txt" }, "notes.txt is not a folder."],
    ["a folder with no write", LOCKED, "You do not have permission to add to locked."],
    ["nothing at all", null, "Drop onto a folder to put files there."],
  ])("refuses %s as the target and sends nothing", (_, target, reason) => {
    expect(moveVerdict([REPORT], target, SOURCE)).toEqual({
      moves: [],
      unchanged: 0,
      refusal: reason,
    });
  });

  it("refuses when the folder the rows came from cannot be written", () => {
    expect(moveVerdict([REPORT], DEST, { name: "Shared with me", canWrite: false })).toEqual({
      moves: [],
      unchanged: 0,
      refusal: "You do not have permission to move items out of Shared with me.",
    });
  });

  it("refuses a folder dropped onto itself", () => {
    const self = { ...DEST, id: MOVER.id, name: "mover", path: "/home/mover" };
    expect(moveVerdict([MOVER], self, SOURCE).refusal).toBe("mover cannot be moved into itself.");
  });

  it("refuses a folder dropped anywhere inside its own subtree, and sends none of the others", () => {
    const inner = { ...DEST, id: "nd_inner", name: "inner", path: "/home/mover/inner" };
    expect(moveVerdict([REPORT, MOVER], inner, SOURCE)).toEqual({
      moves: [],
      unchanged: 0,
      refusal: "mover cannot be moved into a folder inside it.",
    });
  });

  it("does not mistake a sibling whose name merely starts the same for a descendant", () => {
    const sibling = { ...DEST, id: "nd_mover2", name: "mover2", path: "/home/mover2" };
    expect(moveVerdict([MOVER], sibling, SOURCE).moves).toEqual([MOVER]);
  });
});

describe("isInsideSubtree", () => {
  it("a file has no subtree, so nothing is inside it", () => {
    expect(isInsideSubtree({ id: "nd_x", path: "/home/report.csv/x" }, REPORT)).toBe(false);
  });

  it.each([
    ["a sibling whose name extends the folder's", "/home/mover-archive/x", false],
    ["a row deep inside the folder", "/home/mover/a/b", true],
  ])("decides on a path segment boundary: %s", (_label, path, expected) => {
    expect(isInsideSubtree({ id: "nd_x", path }, MOVER)).toBe(expected);
  });

  it("reads a folder path spelled with a trailing slash as the same folder", () => {
    expect(
      isInsideSubtree({ id: "nd_x", path: "/home/mover" }, { ...MOVER, path: "/home/mover/" }),
    ).toBe(true);
  });

  it("answers false when either path is unknown rather than guessing", () => {
    expect(isInsideSubtree({ id: "nd_x", path: null }, MOVER)).toBe(false);
    expect(isInsideSubtree({ id: "nd_x", path: "/home/mover/x" }, { ...MOVER, path: null })).toBe(
      false,
    );
  });
});

describe("what kind of drag is in the air", () => {
  it("tells a desktop drag by the Files type", () => {
    expect(isFilesDrag({ types: ["Files"] })).toBe(true);
    expect(isMoveDrag({ types: ["Files"] })).toBe(false);
  });

  it("tells a row drag by the move type", () => {
    expect(isMoveDrag({ types: [MOVE_MIME] })).toBe(true);
    expect(isFilesDrag({ types: [MOVE_MIME] })).toBe(false);
  });

  it("knows neither dragged text nor an empty transfer", () => {
    expect(isKnownDrag({ types: ["text/plain"] })).toBe(false);
    expect(isKnownDrag({ types: [] })).toBe(false);
    expect(isKnownDrag(null)).toBe(false);
  });
});

describe("leftElement", () => {
  function event(current: HTMLElement, related: EventTarget | null) {
    return { currentTarget: current, relatedTarget: related } as unknown as Parameters<
      typeof leftElement
    >[0];
  }

  it("is a leave when the pointer went to a stranger or off the document", () => {
    const row = document.createElement("div");
    const other = document.createElement("div");
    expect(leftElement(event(row, other))).toBe(true);
    expect(leftElement(event(row, null))).toBe(true);
  });

  it("is not a leave when the pointer only crossed into one of the row's own cells", () => {
    const row = document.createElement("div");
    const cell = document.createElement("span");
    row.appendChild(cell);
    expect(leftElement(event(row, cell))).toBe(false);
  });
});

describe("runPool", () => {
  it("keeps no more than the limit in flight and runs everything in order", async () => {
    const started: number[] = [];
    let inFlight = 0;
    let peak = 0;
    const release: (() => void)[] = [];

    const run = runPool([0, 1, 2, 3, 4], 2, (n) => {
      started.push(n);
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      return new Promise<void>((resolve) => {
        release.push(() => {
          inFlight -= 1;
          resolve();
        });
      });
    });

    expect(started).toEqual([0, 1]);
    while (release.length > 0) {
      release.shift()!();
      await Promise.resolve();
      await Promise.resolve();
    }
    await run;
    expect(started).toEqual([0, 1, 2, 3, 4]);
    expect(peak).toBe(2);
  });

  it("starts nothing more once told to stop, and still settles what was in flight", async () => {
    const started: number[] = [];
    let stop = false;
    await runPool(
      [0, 1, 2, 3],
      1,
      async (n) => {
        started.push(n);
        if (n === 1) stop = true;
      },
      () => stop,
    );
    expect(started).toEqual([0, 1]);
  });

  it("gives every item its turn even when the ones in flight all throw", async () => {
    const started: number[] = [];
    const failed = await runPool([0, 1, 2, 3, 4, 5, 6, 7], 3, async (n) => {
      started.push(n);
      if (n < 3) throw new Error(`boom ${n}`);
    }).then(
      () => null,
      (error: unknown) => error,
    );

    // The three that were in flight died; the five behind them were still asked
    // for — that gap is a batch of files nobody ever uploaded.
    expect(started).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(failed).toBeInstanceOf(AggregateError);
    expect((failed as AggregateError).errors).toHaveLength(3);
  });

  it("runs an empty batch to completion with no work", async () => {
    let calls = 0;
    await runPool([], 3, async () => {
      calls += 1;
    });
    expect(calls).toBe(0);
  });

  // The server tells a part answer how many the caller may keep in flight
  // (`X-Upload-Concurrency`). A pool that read its limit once would keep
  // sending into the shed the whole drop; it is read before every start.
  it("holds a lane back while the live limit is under it, and uses it again when it rises", async () => {
    const started: number[] = [];
    const release: (() => void)[] = [];
    let limit = 3;
    const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

    const run = runPool([0, 1, 2, 3, 4, 5], () => limit, (n) => {
      started.push(n);
      return new Promise<void>((resolve) => release.push(resolve));
    });
    await settle();
    expect(started).toEqual([0, 1, 2]);

    // The share collapses to one while the three are in flight.
    limit = 1;
    while (release.length > 0) release.shift()!();
    await settle();
    expect(started).toEqual([0, 1, 2, 3]);

    limit = 3;
    await new Promise((resolve) => setTimeout(resolve, POOL_HOLD_MS + 20));
    expect(started).toEqual([0, 1, 2, 3, 4, 5]);
    while (release.length > 0) release.shift()!();
    await run;
  });
});

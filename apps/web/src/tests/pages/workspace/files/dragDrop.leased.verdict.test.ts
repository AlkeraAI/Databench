import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import { moveVerdict } from "@/pages/workspace/files/dragDrop";
import { dropVerdict, leasedFolderRefusal, type DropTarget } from "@/pages/workspace/files/dropHandlers";
import { dropTargetOf } from "@/pages/workspace/files/useFilesDragDrop";

// A folder a machine holds refuses a drop the way a folder the reader cannot
// write does: before any request, with a sentence naming the folder. Both
// directions are decided here, with plain data: dropping INTO a held folder,
// and dragging a row OUT of one.

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

const DEST: DropTarget & { path: string } = {
  id: "nd_dest",
  name: "dest",
  kind: "folder",
  capabilities: { can_write: true },
  path: "/home/dest",
};
const HELD = { ...DEST, id: "nd_held", name: "Q3 review", leased: true };
const HELD_AND_LOCKED = { ...HELD, capabilities: { can_write: false } };
const REPORT = item({ id: "nd_report", name: "report.csv" });

describe("dropVerdict on a held folder", () => {
  it("refuses, naming the folder and the lease", () => {
    expect(dropVerdict(HELD)).toEqual({
      accepted: false,
      reason: "Q3 review is leased and read-only until the lease ends.",
    });
  });

  it("names the missing grant first when both refuse: it is the one that outlives the lease", () => {
    expect(dropVerdict(HELD_AND_LOCKED)).toEqual({
      accepted: false,
      reason: "You do not have permission to add to Q3 review.",
    });
  });

  it("accepts the same folder once the flag is off, so the flag is the whole difference", () => {
    expect(dropVerdict({ ...HELD, leased: false })).toEqual({ accepted: true });
    expect(dropVerdict(DEST)).toEqual({ accepted: true });
  });

  // The default the drop and the destination picker share: a folder that says
  // nothing about what the reader may do is not a folder that said yes. Both
  // shapes an incomplete answer takes are pinned.
  it("refuses a folder that never said the reader may write to it", () => {
    const silent = { ...DEST, name: "silent" };
    delete (silent as { capabilities?: unknown }).capabilities;
    expect(dropVerdict(silent)).toEqual({
      accepted: false,
      reason: "You do not have permission to add to silent.",
    });
    expect(dropVerdict({ ...DEST, name: "silent", capabilities: null })).toEqual({
      accepted: false,
      reason: "You do not have permission to add to silent.",
    });
    expect(dropVerdict({ ...DEST, name: "silent", capabilities: {} })).toEqual({
      accepted: false,
      reason: "You do not have permission to add to silent.",
    });
  });
});

describe("moveVerdict and a held folder", () => {
  it("refuses a move INTO a held folder before any row is considered", () => {
    expect(moveVerdict([REPORT], HELD, { name: "home", canWrite: true })).toEqual({
      moves: [],
      unchanged: 0,
      refusal: leasedFolderRefusal("Q3 review"),
    });
  });

  it("refuses a move OUT of a held listing, naming the listing", () => {
    expect(moveVerdict([REPORT], DEST, { name: "Q3 review", canWrite: true, leased: true })).toEqual({
      moves: [],
      unchanged: 0,
      refusal: leasedFolderRefusal("Q3 review"),
    });
  });

  it("names the missing grant on the source before its lease", () => {
    const decision = moveVerdict([REPORT], DEST, { name: "Q3 review", canWrite: false, leased: true });
    expect(decision.refusal).toBe("You do not have permission to move items out of Q3 review.");
  });

  it("moves as before when neither side is held", () => {
    expect(moveVerdict([REPORT], DEST, { name: "home", canWrite: true, leased: false })).toEqual({
      moves: [REPORT],
      unchanged: 0,
      refusal: null,
    });
  });
});

describe("dropTargetOf", () => {
  const folder = item({ id: "nd_f", name: "notes", kind: "folder" });

  it("marks a target leased only when asked, so a surface that takes the write is unchanged", () => {
    expect(dropTargetOf(folder)).toEqual({
      id: "nd_f",
      name: "notes",
      kind: "folder",
      capabilities: { can_write: true },
    });
    expect(dropTargetOf(folder, false)).not.toHaveProperty("leased");
    expect(dropTargetOf(folder, true)).toMatchObject({ id: "nd_f", leased: true });
  });

  it("is nothing for no item, leased or not", () => {
    expect(dropTargetOf(undefined)).toBeNull();
    expect(dropTargetOf(undefined, true)).toBeNull();
  });
});

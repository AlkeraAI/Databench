// "Lease for local use…" needed a form that took a machine and a purpose, and
// that form lived in the details pane. No surface draws one now, so the row
// could only ever have refused — and a row whose one answer is "not available"
// is noise on every right-click. It is out of the menu entirely.
//
// The other three lease rows write straight away, with no form between the
// click and the request, so they stay: these pin that the removal took exactly
// the dead one.

import { describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import {
  buildContextMenuItems,
  type ContextMenuInput,
  type MenuActionId,
} from "@/pages/workspace/files/contextMenuItems";

const HELD_BY_SOMEONE_ELSE = {
  holder: "Robin",
  machine: "MacBook Pro",
  purpose: "mount",
  since: "2026-09-09T10:12:00.000Z",
  expires_at: "2026-09-09T10:42:00.000Z",
  mine: false,
};

function folder(over: Partial<Item> = {}): Item {
  return {
    id: "nd_models",
    ino: 2,
    driveId: "dr_1",
    kind: "folder",
    name: "models",
    nameDisplay: "models",
    nameEncoding: "utf-8",
    parentId: "nd_home",
    pathBytes: "/home/dana/models",
    path: "/home/dana/models",
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    lease: null,
    capabilities: { can_read: true, can_write: true, can_lease: true },
    ...over,
  } as Item;
}

/** The whole menu for one row, as the page builds it. */
function menu(targets: readonly Item[], over: Partial<ContextMenuInput> = {}) {
  return buildContextMenuItems({
    platform: "mac",
    targets,
    currentFolderId: "nd_home",
    canWriteHere: true,
    clipboard: { mode: null, items: [], sourceFolderId: null },
    onAction: vi.fn(),
    ...over,
  } as ContextMenuInput);
}

function rowFor(targets: readonly Item[], id: MenuActionId, over: Partial<ContextMenuInput> = {}) {
  return menu(targets, over).find((row) => row.id === id);
}

describe("the acquire row in the files menu", () => {
  it("is not offered on a folder the caller may lease", () => {
    const rows = menu([folder()]);
    // Absent, not disabled: a disabled row still costs a reader the reading.
    expect(rows.find((row) => row.id === "lease")).toBeUndefined();
    expect(rows.some((row) => row.label.startsWith("Lease for local use"))).toBe(false);
  });

  it("is not offered on a folder somebody else is holding either", () => {
    const rows = menu([folder({ lease: HELD_BY_SOMEONE_ELSE } as unknown as Partial<Item>)]);
    expect(rows.some((row) => row.label.startsWith("Lease for local use"))).toBe(false);
  });

  it("leaves the three rows that still write where they were", () => {
    const held = folder({ lease: HELD_BY_SOMEONE_ELSE } as unknown as Partial<Item>);
    expect(rowFor([held], "request-release", { canForceRelease: true })?.label).toBe(
      "Ask for it back",
    );
    expect(rowFor([held], "force-release", { canForceRelease: true })?.label).toBe("Take back");

    const mine = folder({
      lease: { ...HELD_BY_SOMEONE_ELSE, holder: "you", mine: true },
    } as unknown as Partial<Item>);
    expect(rowFor([mine], "release")?.label).toBe("Release");
  });

  it("dispatches a take-back with the row it was opened over", () => {
    const onAction = vi.fn();
    const held = folder({ lease: HELD_BY_SOMEONE_ELSE } as unknown as Partial<Item>);
    rowFor([held], "force-release", { canForceRelease: true, onAction })?.onSelect?.();
    expect(onAction).toHaveBeenCalledWith("force-release", [held]);
  });
});

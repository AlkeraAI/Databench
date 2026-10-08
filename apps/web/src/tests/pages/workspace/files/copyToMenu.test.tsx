// "Copy to…" on a Files row.
//
// Reading is all a copy takes from its source, so the row is offered on
// anything the reader can read — a colleague's shared file they cannot change,
// a chat somebody opened to them — and refused, with the server's own reason,
// only where they cannot read. It sits right after Move to…, the other row
// that asks for a place, and the trash view keeps its own two rows.

import { describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { buildContextMenuItems, type MenuActionId } from "@/pages/workspace/files/contextMenuItems";
import { emptyClipboard } from "@/pages/workspace/files/state/clipboard";

function item(overrides: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 1,
    driveId: "drv_1",
    kind: "file",
    name: "shared.txt",
    nameDisplay: "shared.txt",
    nameEncoding: "utf-8",
    parentId: "nd_parent",
    etag: "1",
    capabilities: {
      can_read: true,
      can_write: false,
      can_share: false,
      can_delete: false,
      can_purge: false,
      can_rename: false,
      can_download: true,
      can_lease: false,
      can_lease_request: false,
      can_lease_force: false,
      refusals: {},
    },
    ...overrides,
  } as Item;
}

function menu(targets: Item[], extra: { inTrash?: boolean } = {}) {
  return buildContextMenuItems({
    platform: "mac",
    targets,
    currentFolderId: "nd_parent",
    canWriteHere: false,
    clipboard: emptyClipboard,
    onAction: () => {},
    ...extra,
  });
}

const ids = (rows: ReturnType<typeof menu>): MenuActionId[] => rows.map((row) => row.id as MenuActionId);

describe("Copy to… on a Files row", () => {
  it("is offered on a row the reader may only read, right after Move to…", () => {
    const rows = menu([item()]);
    const copyTo = rows.find((row) => row.id === "copy-to");
    expect(copyTo?.label).toBe("Copy to…");
    expect(copyTo?.disabled).toBeUndefined();
    // The row that cannot be changed still copies; the row that moves it is greyed.
    expect(rows.find((row) => row.id === "move-to")?.disabled).toBeDefined();
    expect(ids(rows).indexOf("copy-to")).toBe(ids(rows).indexOf("move-to") + 1);
  });

  it("carries the server's own reason where the reader cannot read", () => {
    const unreadable = item({
      capabilities: {
        can_read: false,
        can_write: false,
        can_share: false,
        can_delete: false,
        can_purge: false,
        can_rename: false,
        can_download: false,
        can_lease: false,
        can_lease_request: false,
        can_lease_force: false,
        refusals: { read: "This item is not shared with you." },
      },
    } as Partial<Item>);
    const copyTo = menu([unreadable]).find((row) => row.id === "copy-to");
    expect(copyTo?.disabled).toBe("This item is not shared with you.");
  });

  it("names the first row of a selection that refuses", () => {
    const second = item({
      id: "nd_2",
      capabilities: {
        can_read: false,
        can_write: false,
        can_share: false,
        can_delete: false,
        can_purge: false,
        can_rename: false,
        can_download: false,
        can_lease: false,
        can_lease_request: false,
        can_lease_force: false,
        refusals: {},
      },
    } as Partial<Item>);
    const copyTo = menu([item(), second]).find((row) => row.id === "copy-to");
    expect(copyTo?.disabled).toBe("You do not have access to this item.");
  });

  it("is out of the menu with nothing selected, and out of the trash view", () => {
    // Nothing to copy, so nothing to explain: the row that acts on a selection
    // is not drawn when there is none.
    expect(menu([]).find((row) => row.id === "copy-to")).toBeUndefined();
    expect(ids(menu([item()], { inTrash: true }))).not.toContain("copy-to");
  });
});

// "Copy link" on a Files row.
//
// Sharing and linking are halves of the same errand — handing one thing to
// somebody else — so the row sits directly under Share… rather than being
// reachable only from inside the dialog. Reading is all a link takes: the link
// is an address, not the access, so a row somebody may only read still offers
// it; a row they cannot read does not, in the server's own words.

describe("Copy link on a Files row", () => {
  it.each([
    ["a file", item()],
    ["a folder", item({ id: "nd_dir", kind: "folder", name: "reports", nameDisplay: "reports" })],
  ])("follows Share… on %s", (_label, row) => {
    const rows = menu([row]);
    expect(rows.find((one) => one.id === "copy-link")?.label).toBe("Copy link");
    expect(ids(rows).indexOf("copy-link")).toBe(ids(rows).indexOf("share") + 1);
  });

  it("is offered on a row the reader may only read — the link is not the access", () => {
    // The fixture cannot share and cannot write; it can read. Share… is greyed
    // and Copy link is not, which is the whole distinction.
    const rows = menu([item()]);
    expect(rows.find((one) => one.id === "copy-link")?.disabled).toBeUndefined();
    expect(rows.find((one) => one.id === "share")?.disabled).toBeDefined();
  });

  it("carries the server's own reason where the reader cannot read", () => {
    const unreadable = item({
      capabilities: {
        can_read: false,
        can_write: false,
        can_share: false,
        can_delete: false,
        can_purge: false,
        can_rename: false,
        can_download: false,
        can_lease: false,
        can_lease_request: false,
        can_lease_force: false,
        refusals: { read: "This item is not shared with you." },
      },
    } as Partial<Item>);
    expect(menu([unreadable]).find((one) => one.id === "copy-link")?.disabled).toBe(
      "This item is not shared with you.",
    );
  });

  it("is drawn only for a single row: a link points at one thing", () => {
    expect(menu([]).find((one) => one.id === "copy-link")).toBeUndefined();
    expect(menu([item(), item({ id: "nd_2" })]).find((one) => one.id === "copy-link")).toBeUndefined();
  });

  it("is absent in the trash view", () => {
    expect(ids(menu([item()], { inTrash: true }))).not.toContain("copy-link");
  });

  it("dispatches copy-link with the row the menu was opened over", () => {
    const onAction = vi.fn();
    buildContextMenuItems({
      platform: "mac",
      targets: [item()],
      currentFolderId: "nd_parent",
      canWriteHere: false,
      clipboard: emptyClipboard,
      onAction,
    })
      .find((one) => one.id === "copy-link")
      ?.onSelect?.();
    expect(onAction).toHaveBeenCalledWith("copy-link", [item()]);
  });
});

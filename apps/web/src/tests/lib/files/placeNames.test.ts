// The folder the product files a member's saved templates in is stored as
// `Chat Templates`; the UI states names in sentence case. The re-casing is display
// only and applies to that folder alone: a folder a person made and called
// the same thing somewhere else keeps the casing they gave it.

import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import { displayNameOf } from "@/lib/files/columns";

/** A folder at `pathBytes`, carrying the home its path runs through the way the
 *  server sends it: a home is stored under its owner's id, and `pathHome` names it. */
function folder(pathBytes: string, over: Partial<Item> = {}): Item {
  const segments = pathBytes.split("/");
  const name = segments[segments.length - 1] ?? "";
  const inHome = segments[1] === "home" && segments[2] !== undefined;
  const homeId = `nd_home_${segments[2] ?? ""}`;
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "folder",
    name,
    nameDisplay: name,
    nameEncoding: "utf-8",
    pathBytes,
    parentId: inHome && segments.length === 4 ? homeId : "nd_parent",
    pathHome: inHome ? { node_id: homeId, owner_id: segments[2] ?? "", owner_name: "Dana Ruiz" } : null,
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: { can_read: true, can_write: true, can_share: true, can_delete: true },
    ...over,
  } as Item;
}

describe("the product's own folder names", () => {
  it("shows the chat templates folder in a member's home in sentence case", () => {
    const templates = folder("/home/dana/Chat Templates");
    expect(displayNameOf(templates)).toBe("Chat templates");
    // Display only: the stored name is what renames and moves still address.
    expect(templates.name).toBe("Chat Templates");
  });

  it.each([
    ["deeper in the home", "/home/dana/projects/Chat Templates"],
    ["in a team folder", "/Teams/Data/Chat Templates"],
    ["in Shared", "/Shared/Chat Templates"],
  ])("keeps a person's own folder %s as they named it", (_where, path) => {
    expect(displayNameOf(folder(path))).toBe("Chat Templates");
  });

  it("keys the place on the home the server named, not on the path's spelling", () => {
    // A path that merely reads like a home's, with no home named for it, is not one.
    expect(displayNameOf(folder("/home/dana/Chat Templates", { pathHome: null }))).toBe("Chat Templates");
  });

  it("keeps a file of that name as it is", () => {
    expect(displayNameOf(folder("/home/dana/Chat Templates", { kind: "file" }))).toBe("Chat Templates");
  });

  it("leaves Chats, already in sentence case, alone", () => {
    expect(displayNameOf(folder("/home/dana/Chats"))).toBe("Chats");
  });
});

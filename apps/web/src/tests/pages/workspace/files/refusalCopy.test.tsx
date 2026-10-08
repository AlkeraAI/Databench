// Why a Files control is refused, as a person reads it.
//
// The server files each refusal under the action it decides, as a reason CODE
// (`insufficient_role`, `no_reshare`, `held`, …). The context menu printed that
// code as the disabled row's reason, so a viewer read "insufficient_role" under
// Share…, Move to…, Cut and Move to trash. Every refusal now reads as a
// sentence, one that names the reader's rung when the rung is the reason.

import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import { buildContextMenuItems } from "@/pages/workspace/files/contextMenuItems";
import { CAP_REFUSAL, capabilityRefusal } from "@/pages/workspace/files/refusalCopy";
import { emptyClipboard } from "@/pages/workspace/files/state/clipboard";

type Caps = NonNullable<Item["capabilities"]>;

function item(caps: Partial<Caps>): Item {
  return {
    id: "nd_1",
    ino: 1,
    driveId: "drv_1",
    kind: "folder",
    name: "shared",
    nameDisplay: "shared",
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
      ...caps,
    },
  } as Item;
}

/** What the server sends a reader shared at Can view: every change refused
 *  for the rung. */
const VIEWER = item({
  refusals: {
    write: "insufficient_role",
    share: "insufficient_role",
    delete: "insufficient_role",
  },
});
/** And at Can edit: only sharing is above the rung. */
const EDITOR = item({
  can_write: true,
  can_delete: true,
  can_rename: true,
  refusals: { share: "insufficient_role" },
});

function reasons(target: Item): Record<string, string | undefined> {
  const rows = buildContextMenuItems({
    platform: "mac",
    targets: [target],
    currentFolderId: "nd_parent",
    canWriteHere: false,
    clipboard: emptyClipboard,
    onAction: () => {},
  });
  return Object.fromEntries(rows.map((row) => [row.id, row.disabled]));
}

describe("the context menu's reasons", () => {
  it("names the viewer's rung and the refused action, never the code", () => {
    const why = reasons(VIEWER);
    expect(why.share).toBe("You can view this, not share it.");
    expect(why["move-to"]).toBe("You can view this, not move it.");
    expect(why.cut).toBe("You can view this, not move it.");
    expect(why.trash).toBe("You can view this, not trash it.");
    for (const reason of Object.values(why)) expect(reason ?? "").not.toMatch(/insufficient_role/);
  });

  it("names the editor's rung where only sharing is above it", () => {
    const why = reasons(EDITOR);
    expect(why.share).toBe("You can edit this, not share it.");
    expect(why["move-to"]).toBeUndefined();
    expect(why.trash).toBeUndefined();
  });
});

describe("capabilityRefusal", () => {
  it.each([
    ["no_reshare", "Resharing is turned off for this item."],
    ["held", "This is on legal hold."],
    ["files.held", "This is on legal hold."],
    ["frozen", "This drive is over its storage limit."],
  ])("words the state reason %s", (code, sentence) => {
    expect(capabilityRefusal(item({ refusals: { share: code } }), "can_share")).toBe(sentence);
  });

  it("keeps a reason the server already wrote as a sentence", () => {
    const said = "The chat owns this file.";
    expect(capabilityRefusal(item({ refusals: { write: said } }), "can_write")).toBe(said);
  });

  it.each(["no_role", "not_in_org", "agent_confined", "some_future_code"])(
    "falls back to the capability's own sentence for %s",
    (code) => {
      expect(capabilityRefusal(item({ refusals: { share: code } }), "can_share")).toBe(
        CAP_REFUSAL.can_share,
      );
    },
  );

  it("does not name a rung the reader does not hold", () => {
    const none = item({ can_read: false, refusals: { share: "insufficient_role" } });
    expect(capabilityRefusal(none, "can_share")).toBe(CAP_REFUSAL.can_share);
  });

  it("answers nothing for a capability that is granted", () => {
    expect(capabilityRefusal(EDITOR, "can_write")).toBeUndefined();
  });
});

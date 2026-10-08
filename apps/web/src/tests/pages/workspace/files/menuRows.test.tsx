import { describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { buildContextMenuItems, type MenuActionId } from "@/pages/workspace/files/contextMenuItems";

// The Files row menu is the whole product surface for the two halves of a
// template: saving a chat as one, and starting a chat from one.
//
// Two kinds of "no" and they are answered differently. An action that does not
// APPLY to what was clicked — saving a plain file as a template, leasing a file,
// renaming three rows at once — is not in the menu at all: a sentence explaining
// that a file is not a chat is noise on every right-click. An action that applies
// but is REFUSED — the caller may not read this chat, the folder is leased — stays
// in the menu, disabled, carrying the reason, because that is a fact about this
// row that a person needs.

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "notes.txt",
    nameDisplay: "notes.txt",
    etag: "et_1",
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
    },
    ...over,
  } as Item;
}

const FILE = item();

const CHAT = item({
  id: "nd_chat",
  kind: "folder",
  name: "Q3 review.alkerachat",
  nameDisplay: "Q3 review.alkerachat",
  object: { type: "chat", id: "cht_9", title: "Q3 review", web_url: "/chat/cht_9" },
} as unknown as Partial<Item>);

const TEMPLATE = item({
  id: "nd_tpl",
  kind: "folder",
  name: "Monthly revenue.alkerachat.template",
  nameDisplay: "Monthly revenue.alkerachat.template",
  object: {
    type: "chat_template",
    id: "tpl_4",
    title: "Monthly revenue",
    web_url: "/templates/tpl_4",
  },
} as unknown as Partial<Item>);

const WORKSPACE = item({
  id: "nd_ws",
  kind: "folder",
  name: "Pricing.alkeraworkspace",
  nameDisplay: "Pricing.alkeraworkspace",
  object: { type: "workspace", id: "ws_3", title: "Pricing", web_url: "/workspaces/ws_3" },
} as unknown as Partial<Item>);

const FOLDER = item({ id: "nd_folder", kind: "folder", name: "reports", nameDisplay: "reports" });

function menu(targets: readonly Item[], onAction = vi.fn()) {
  return buildContextMenuItems({
    platform: "mac",
    targets,
    currentFolderId: "nd_home",
    canWriteHere: true,
    clipboard: { mode: null, items: [], sourceFolderId: null },
    onAction,
  });
}

function rowFor(targets: readonly Item[], id: MenuActionId, onAction = vi.fn()) {
  return menu(targets, onAction).find((row) => row.id === id);
}

describe("Save as template… on a Files row", () => {
  it("is offered on a chat", () => {
    expect(rowFor([CHAT], "save-as-template")?.disabled).toBeUndefined();
  });

  it.each([
    ["a plain file", FILE],
    ["a plain folder", FOLDER],
    ["a template", TEMPLATE],
  ])("is not in the menu on %s: only a chat has one to save", (_label, target) => {
    expect(rowFor([target], "save-as-template")).toBeUndefined();
  });

  it("refuses a chat the caller cannot read, in the server's own words", () => {
    const unreadable = item({
      ...CHAT,
      capabilities: {
        can_read: false,
        refusals: { read: "This chat was shared with someone else." },
      },
    } as unknown as Partial<Item>);
    expect(rowFor([unreadable], "save-as-template")?.disabled).toBe(
      "This chat was shared with someone else.",
    );
  });

  it("is not in the menu on a plural selection: a template is saved from one chat", () => {
    expect(rowFor([CHAT, CHAT], "save-as-template")).toBeUndefined();
  });

  it("dispatches the action with the row it was opened over", () => {
    const onAction = vi.fn();
    rowFor([CHAT], "save-as-template", onAction)?.onSelect?.();
    expect(onAction).toHaveBeenCalledWith("save-as-template", [CHAT]);
  });
});

describe("New chat from template on a Files row", () => {
  it("is offered on a template", () => {
    expect(rowFor([TEMPLATE], "new-chat-from-template")?.disabled).toBeUndefined();
  });

  it.each([
    ["a chat", CHAT],
    ["a plain file", FILE],
    ["a plain folder", FOLDER],
  ])("is not in the menu on %s: only a template starts one", (_label, target) => {
    expect(rowFor([target], "new-chat-from-template")).toBeUndefined();
  });

  it("dispatches the action with the template row", () => {
    const onAction = vi.fn();
    rowFor([TEMPLATE], "new-chat-from-template", onAction)?.onSelect?.();
    expect(onAction).toHaveBeenCalledWith("new-chat-from-template", [TEMPLATE]);
  });
});

describe("the rows that read off being a page AND a folder", () => {
  it.each([
    ["a chat", CHAT, "Open chat"],
    ["a workspace, whose Open lists its files,", WORKSPACE, "Open"],
    ["a template", TEMPLATE, "Open template"],
    ["a plain folder", FOLDER, "Open"],
    ["a file", FILE, "Open"],
  ])("names Open for what %s is", (_label, target, label) => {
    expect(rowFor([target], "open")?.label).toBe(label);
  });

  it.each([
    ["a chat", CHAT],
    ["a template", TEMPLATE],
  ])("offers Browse files on %s", (_label, target) => {
    const row = rowFor([target], "view-files");
    expect(row?.label).toBe("Browse files");
    expect(row?.disabled).toBeUndefined();
  });

  it.each([
    ["a plain folder", FOLDER],
    ["a file", FILE],
    ["a workspace, whose Open lists its files", WORKSPACE],
  ])("drops Browse files on %s: it already is its files", (_label, target) => {
    expect(rowFor([target], "view-files")).toBeUndefined();
  });

  it("offers Open workspace on a workspace and dispatches it with the row", () => {
    const onAction = vi.fn();
    const row = rowFor([WORKSPACE], "open-page", onAction);
    expect(row?.label).toBe("Open workspace");
    row?.onSelect?.();
    expect(onAction).toHaveBeenCalledWith("open-page", [WORKSPACE]);
  });

  it.each([
    ["a chat, whose Open is its page", CHAT],
    ["a template, whose Open is its page", TEMPLATE],
    ["a plain folder", FOLDER],
    ["a file", FILE],
  ])("offers no second page row on %s", (_label, target) => {
    expect(rowFor([target], "open-page")).toBeUndefined();
  });

  // Open in the menu is the same gesture as a double-click and Enter: the page
  // decides what it means for the row, once, so the menu never runs its own.
  it.each([
    ["a chat", CHAT],
    ["a workspace", WORKSPACE],
    ["a template", TEMPLATE],
    ["a plain folder", FOLDER],
    ["a file", FILE],
  ])("runs Open on %s as the one open every gesture runs", (_label, target) => {
    const onAction = vi.fn();
    rowFor([target], "open", onAction)?.onSelect?.();
    expect(onAction).toHaveBeenCalledWith("open", [target]);
  });
});

describe("what Move to trash says it is moving", () => {
  it("says the chat's FILES on a chat, because the chat is deleted elsewhere", () => {
    expect(rowFor([CHAT], "trash")?.label).toBe("Move the chat's files to trash");
  });

  it.each([
    ["a template", TEMPLATE],
    ["a plain folder", FOLDER],
    ["a file", FILE],
  ])("says Move to trash on %s", (_label, target) => {
    expect(rowFor([target], "trash")?.label).toBe("Move to trash");
  });
});

describe("the retired row", () => {
  it("offers nothing to re-run a saved report or query", () => {
    for (const target of [FILE, FOLDER, CHAT, TEMPLATE]) {
      expect(menu([target]).map((row) => row.id)).not.toContain("start-chat");
    }
  });
});

describe("what a menu offers when the action cannot apply", () => {
  /** Every row the menu is willing to show for these targets. */
  function ids(targets: readonly Item[]): MenuActionId[] {
    return menu(targets).map((row) => row.id as MenuActionId);
  }

  it("offers only what acts on the FOLDER when nothing is selected", () => {
    // A right-click on empty space acts on the folder that was clicked in. A row
    // that needs a target is not listed at all.
    expect(ids([])).toEqual(["paste", "new-folder", "upload-files", "upload-folder"]);
  });

  it("drops the single-item rows from a plural selection, and keeps the plural ones", () => {
    const plural = ids([FILE, FOLDER]);
    for (const single of [
      "rename",
      "share",
      "copy-link",
      "versions",
      "details",
      "open",
      "open-new-tab",
    ]) {
      expect(plural, `${single} cannot act on two rows`).not.toContain(single);
    }
    for (const many of ["copy", "cut", "move-to", "copy-to", "duplicate", "download", "trash"]) {
      expect(plural, `${many} acts on a selection`).toContain(many);
    }
  });

  it("keeps a row the caller simply may not use, with the reason on it", () => {
    const sealed = item({
      capabilities: {
        can_read: true,
        can_rename: false,
        can_download: false,
        refusals: { rename: "This file is published and cannot be renamed." },
      },
    } as unknown as Partial<Item>);
    // Renaming applies to a file; this caller may not do it. That is a fact
    // about the row, so it is said rather than hidden.
    expect(rowFor([sealed], "rename")?.disabled).toBe(
      "This file is published and cannot be renamed.",
    );
    expect(rowFor([sealed], "download")?.disabled).toBe("You cannot download this item.");
  });

  it("keeps the rows a lease refuses, because the lease ends", () => {
    const held = item({ id: "nd_held", kind: "folder" });
    const rows = buildContextMenuItems({
      platform: "mac",
      targets: [held],
      currentFolderId: "nd_home",
      canWriteHere: true,
      leasedHere: true,
      isHeld: () => true,
      clipboard: { mode: null, items: [], sourceFolderId: null },
      onAction: vi.fn(),
    });
    const row = (id: MenuActionId) => rows.find((entry) => entry.id === id);
    expect(row("rename")?.disabled).toBe(
      "This item is in a leased folder and is read-only until the lease ends.",
    );
    expect(row("new-folder")?.disabled).toBe(
      "This folder is leased and read-only until the lease ends.",
    );
  });

  it("offers exactly one of the lease rows, for the state the folder is in", () => {
    // A folder nobody is holding offers none of them: the acquire row is out of
    // the menu, and the three that get a folder back have nothing to act on.
    const free = item({ id: "nd_f", kind: "folder" });
    for (const absent of ["lease", "release", "request-release", "force-release"]) {
      expect(ids([free])).not.toContain(absent);
    }

    const mine = item({ id: "nd_m", kind: "folder", lease: { mine: true } } as Partial<Item>);
    expect(ids([mine])).toContain("release");
    expect(ids([mine])).not.toContain("lease");
    expect(ids([mine])).not.toContain("request-release");

    const theirs = item({ id: "nd_t", kind: "folder", lease: { mine: false } } as Partial<Item>);
    expect(ids([theirs])).toContain("request-release");
    expect(ids([theirs])).toContain("force-release");
    expect(ids([theirs])).not.toContain("release");
    // Taking a folder back is something a manager may do and others may not —
    // a permission, so it is named rather than hidden.
    expect(rowFor([theirs], "force-release")?.disabled).toBe(
      "Only a manager can take a folder back.",
    );
  });

  it("never offers a lease row on a file", () => {
    for (const id of ["lease", "release", "request-release", "force-release"]) {
      expect(ids([FILE])).not.toContain(id);
    }
  });
});

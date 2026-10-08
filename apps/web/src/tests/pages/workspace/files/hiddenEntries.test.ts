import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import {
  chatRecordsRule,
  entryVisibility,
  isSystemEntry,
  visibleRows,
} from "@/pages/workspace/files/hiddenEntries";

// System entries a person does not read (dot names, a tool's own state folder)
// are hidden at every depth unless the viewer asks to see them; a chat
// folder's box records are hidden whatever the viewer asks.

function row(name: string, over: Partial<Item> = {}): Item {
  return { id: `nd_${name}`, kind: "file", name, nameDisplay: name, ...over } as Item;
}

const folder = (name: string, over: Partial<Item> = {}) => row(name, { kind: "folder", ...over });

const CHAT = {
  id: "nd_chat",
  kind: "folder",
  name: "c.alkerachat",
  nameDisplay: "c.alkerachat",
  object: {
    type: "chat",
    id: "cht_1",
    web_url: "/chat/cht_1",
    metadata: { files_node_id: "nd_work" },
  },
} as unknown as Item;

describe("which entries are system entries", () => {
  it.each([
    ["a dot file", row(".gitignore"), true],
    ["a dot folder", folder(".venv"), true],
    ["the marimo state folder", folder("__marimo__"), true],
    ["a dot file deep in a tree", row(".DS_Store", { parentId: "nd_deep_in_a_tree" }), true],
    ["a folder named marimo", folder("marimo"), false],
    ["a FILE named __marimo__", row("__marimo__"), false],
    ["a folder whose name only contains __marimo__", folder("__marimo__old"), false],
    ["a folder whose name only ends in a dot segment", folder("site.config"), false],
    ["an ordinary notebook", row("analysis.py"), false],
    ["the session folder inside __marimo__", folder("session"), false],
  ])("%s", (_label, item, expected) => {
    expect(isSystemEntry(item)).toBe(expected);
  });

  it("reads the display name before the stored one", () => {
    expect(isSystemEntry({ kind: "file", name: "x", nameDisplay: ".env" } as Item)).toBe(true);
    expect(isSystemEntry({ kind: "file", name: ".env", nameDisplay: "env" } as Item)).toBe(false);
  });

  it("falls back to the stored name when there is no display name", () => {
    expect(isSystemEntry({ kind: "folder", name: "__marimo__", nameDisplay: "" } as Item)).toBe(true);
  });
});

describe("how a row appears", () => {
  it.each([
    ["a normal row, toggle off", row("a.py"), false, "shown"],
    ["a normal row, toggle on", row("a.py"), true, "shown"],
    ["a dot file, toggle off", row(".env"), false, "omitted"],
    ["a dot file, toggle on", row(".env"), true, "dimmed"],
    ["__marimo__, toggle off", folder("__marimo__"), false, "omitted"],
    ["__marimo__, toggle on", folder("__marimo__"), true, "dimmed"],
  ] as const)("%s", (_label, item, showHidden, expected) => {
    expect(entryVisibility(item, undefined, showHidden)).toBe(expected);
  });

  it("keeps a chat folder's box records out even with the toggle on", () => {
    const rule = chatRecordsRule(CHAT);
    expect(rule).toBeDefined();
    for (const showHidden of [false, true]) {
      expect(entryVisibility(row("chat.jsonl"), rule, showHidden)).toBe("omitted");
      expect(entryVisibility(folder(".runtime"), rule, showHidden)).toBe("omitted");
    }
    expect(entryVisibility(folder("scratch", { id: "nd_work" }), rule, false)).toBe("shown");
  });

  it("names no records rule for a folder that is not a chat", () => {
    expect(chatRecordsRule(folder("plain"))).toBeUndefined();
    expect(chatRecordsRule(undefined)).toBeUndefined();
  });

  it("filters a listing in order", () => {
    const rows = [row("a.py"), folder("__marimo__"), row(".gitignore"), folder("marimo")];
    expect(visibleRows(rows, undefined, false).map((r) => r.name)).toEqual(["a.py", "marimo"]);
    expect(visibleRows(rows, undefined, true).map((r) => r.name)).toEqual([
      "a.py",
      "__marimo__",
      ".gitignore",
      "marimo",
    ]);
  });
});

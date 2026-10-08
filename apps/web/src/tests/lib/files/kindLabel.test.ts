import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import { kindLabel, navIconFor } from "@/lib/files/columns";
import { NOTEBOOK_SUFFIX } from "@/lib/files/fileTypes";

/** A chat, a query or a report is a FOLDER that carries the object facet; the
 *  Kind column must read the facet, not the node kind. */
function item(overrides: Partial<Item>): Item {
  return {
    id: "nd_1",
    name: "x",
    nameDisplay: "x",
    kind: "folder",
    ...overrides,
  } as Item;
}

describe("kindLabel", () => {
  it.each([
    ["chat", "Chat"],
    ["query", "Query"],
    ["report", "Report"],
  ])("an object-backed folder of type %s reads %s", (type, label) => {
    expect(kindLabel(item({ kind: "folder", object: { type } as Item["object"] }))).toBe(label);
  });

  it("a plain folder still reads Folder, and a file its extension", () => {
    expect(kindLabel(item({ kind: "folder" }))).toBe("Folder");
    expect(kindLabel(item({ kind: "file", name: "receipt.pdf", nameDisplay: "receipt.pdf" }))).toBe(
      "PDF file",
    );
  });

  it("an object node without a facet type falls back to its subtype", () => {
    expect(kindLabel(item({ kind: "object", subtype: "note" }))).toBe("Note");
    expect(kindLabel(item({ kind: "object" }))).toBe("Object");
  });

  it("a chat template reads Template, not the facet's own spelling", () => {
    // Title-casing the wire name gives "Chat_template", which is a thing nobody
    // calls one — the Kind column is what a person reads, so it says Template
    // whichever shape the row arrives in.
    expect(kindLabel(item({ object: { type: "chat_template" } as Item["object"] }))).toBe(
      "Template",
    );
    expect(kindLabel(item({ kind: "object", subtype: "chat_template" }))).toBe("Template");
  });
});

describe("navIconFor", () => {
  it("gives a template a mark of its own, not the chat's", () => {
    // A template double-clicked in mistake for a chat starts a whole new
    // conversation, so the two rows must not wear the same glyph.
    const chat = item({ object: { type: "chat" } as Item["object"] });
    const template = item({ object: { type: "chat_template" } as Item["object"] });
    expect(navIconFor(chat)).toBe("chats");
    expect(navIconFor(template)).toBe("template");
    expect(navIconFor(template)).not.toBe(navIconFor(chat));
  });

  it("is null for a row that is only itself, and for an object with no page glyph", () => {
    expect(navIconFor(item({ kind: "folder" }))).toBeNull();
    expect(navIconFor(item({ object: { type: "report" } as Item["object"] }))).toBeNull();
  });
});

describe("an Notebook in the Files list", () => {
  const file = (name: string) => item({ kind: "file", name, nameDisplay: name });

  it("reads Notebook, matched before its .py ending", () => {
    expect(kindLabel(file(`analysis${NOTEBOOK_SUFFIX}`))).toBe("Notebook");
    expect(kindLabel(file("ANALYSIS.ALKNB.PY"))).toBe("Notebook");
  });

  it("leaves a plain Python file, and a file named only the suffix, alone", () => {
    expect(kindLabel(file("build.py"))).toBe("PY file");
    expect(kindLabel(file("notes.alknb.txt"))).toBe("TXT file");
  });

});

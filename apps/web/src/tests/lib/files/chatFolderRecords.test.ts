import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import { hiddenInChatFolder } from "@/lib/files/chatFolder";

// A chat folder carries the box's own records beside the chat's work: they
// travel through the drive so the next box can pick the chat up, and they are
// never a person's files. What is hidden follows the folder's layout, so a
// record the box starts writing tomorrow is hidden without a new name here.

function row(name: string, over: Partial<Item> = {}): Item {
  return { id: `nd_${name}`, kind: "file", name, nameDisplay: name, ...over } as Item;
}

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

const OLD_CHAT = {
  ...CHAT,
  object: { type: "chat", id: "cht_2", web_url: "/chat/cht_2" },
} as unknown as Item;

const WORK = row("scratch", { id: "nd_work", kind: "folder" });

describe("what a chat folder's listing keeps out of sight", () => {
  it.each([
    ["chat.jsonl", row("chat.jsonl")],
    ["decisions.jsonl", row("decisions.jsonl")],
    ["manifest.json", row("manifest.json")],
    ["trace.digest.json", row("trace.digest.json")],
    ["a record the box writes later", row("intents.jsonl")],
    [".runtime", row(".runtime", { kind: "folder" })],
    [".lock", row(".lock")],
  ])("hides %s beside a named working directory", (_label, item) => {
    expect(hiddenInChatFolder(item, CHAT)).toBe(true);
  });

  it("shows the working directory itself", () => {
    expect(hiddenInChatFolder(WORK, CHAT)).toBe(false);
  });

  it("shows a folder an older chat kept its files in", () => {
    expect(hiddenInChatFolder(row("outputs", { kind: "folder" }), CHAT)).toBe(false);
  });

  it("hides a dot-folder even though it is a folder", () => {
    expect(hiddenInChatFolder(row(".overlay", { kind: "folder" }), CHAT)).toBe(true);
  });

  it("keeps the files of a chat that names no working directory, its dot-entries still hidden", () => {
    expect(hiddenInChatFolder(row("draft.md"), OLD_CHAT)).toBe(false);
    expect(hiddenInChatFolder(row(".runtime", { kind: "folder" }), OLD_CHAT)).toBe(true);
  });
});

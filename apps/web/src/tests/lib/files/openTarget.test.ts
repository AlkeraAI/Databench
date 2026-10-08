// The one answer to "what does opening this row do", per row kind. Every
// gesture (double-click, Enter, the menu's Open, the details pane, a search
// hit, Open in new tab) reads it, so each kind is pinned here once.

import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import {
  newTabHrefOf,
  openLabelOf,
  openTargetOf,
  pageDoorOf,
  secondPageDoorOf,
  type OpenTarget,
  type PageDoor,
} from "@/lib/files/openTarget";

function row(over: Partial<Item> & Record<string, unknown>): Item {
  return {
    id: "nd_1",
    driveId: "dr_1",
    kind: "file",
    name: "notes.txt",
    nameDisplay: "notes.txt",
    ...over,
  } as unknown as Item;
}

function folderObject(type: string, webUrl: string | null, filesNode?: string): Item {
  return row({
    id: `nd_${type}`,
    kind: "folder",
    name: `${type}.folder`,
    object: {
      type,
      id: `${type}_7`,
      title: type,
      web_url: webUrl,
      metadata: filesNode === undefined ? {} : { files_node_id: filesNode },
    },
  });
}

const CHAT = folderObject("chat", "/chat/chat_7", "nd_chat_scratch");
const WORKSPACE = folderObject("workspace", "/workspaces/workspace_7", "nd_ws_files");
const TEMPLATE = folderObject("chat_template", "/templates/chat_template_7", "nd_tpl_scratch");
const RESULT = row({
  id: "nd_result",
  kind: "object",
  name: "Top accounts.alkeraresult",
  object: {
    type: "result",
    id: "res_2",
    title: "Top accounts",
    web_url: "/objects/res_2",
    metadata: {},
  },
});
const FOLDER = row({ id: "nd_plain", kind: "folder", name: "papers" });
const FILE = row({ id: "nd_csv", name: "rows.csv" });
const NOTEBOOK = row({ id: "nd_nb", name: "Churn.alknb.py" });

describe("openTargetOf", () => {
  it.each<[string, Item, OpenTarget]>([
    ["a chat opens the conversation", CHAT, { kind: "page", to: "/chat/chat_7" }],
    [
      "a workspace lists its files like a folder",
      WORKSPACE,
      { kind: "folder", nodeId: "nd_ws_files" },
    ],
    [
      "a workspace with no files node lists itself",
      folderObject("workspace", "/workspaces/w"),
      { kind: "folder", nodeId: "nd_workspace" },
    ],
    ["a template opens its brief", TEMPLATE, { kind: "page", to: "/templates/chat_template_7" }],
    ["a promoted result opens the object page", RESULT, { kind: "page", to: "/objects/res_2" }],
    ["a plain folder is listed", FOLDER, { kind: "folder", nodeId: "nd_plain" }],
    ["a file goes to the viewer", FILE, { kind: "file" }],
    ["a notebook is a file to the viewer", NOTEBOOK, { kind: "file" }],
  ])("%s", (_why, item, expected) => {
    expect(openTargetOf(item)).toEqual(expected);
  });

  it.each<[string, Item, string]>([
    ["a template", folderObject("chat_template", null, "nd_tpl_scratch"), "nd_tpl_scratch"],
    ["a workspace", folderObject("workspace", null, "nd_ws_files"), "nd_ws_files"],
    ["a chat with no working directory", folderObject("chat", null), "nd_chat"],
  ])("opens %s whose deployment names no page on its files", (_why, item, nodeId) => {
    expect(openTargetOf(item)).toEqual({ kind: "folder", nodeId });
  });

  it("does not take a folder named like a chat for one", () => {
    const named = row({ id: "nd_fake", kind: "folder", name: "Q3.alkerachat" });
    expect(openTargetOf(named)).toEqual({ kind: "folder", nodeId: "nd_fake" });
  });
});

describe("openLabelOf", () => {
  it.each<[string, Item | undefined, string]>([
    ["a chat", CHAT, "Open chat"],
    ["a workspace, whose Open lists its files", WORKSPACE, "Open"],
    ["a template", TEMPLATE, "Open template"],
    ["a template with no page", folderObject("chat_template", null), "Open"],
    ["a result", RESULT, "Open"],
    ["a folder", FOLDER, "Open"],
    ["a file", FILE, "Open"],
    ["nothing", undefined, "Open"],
  ])("names %s", (_why, item, label) => {
    expect(openLabelOf(item)).toBe(label);
  });
});

describe("newTabHrefOf", () => {
  it.each<[string, Item, string]>([
    ["a chat's page", CHAT, "/chat/chat_7"],
    ["a workspace's files", WORKSPACE, "/files/nd_ws_files"],
    ["a folder's listing", FOLDER, "/files/nd_plain"],
    ["a file's own address", FILE, "/files/nd_csv"],
  ])("opens %s", (_why, item, href) => {
    expect(newTabHrefOf(item)).toBe(href);
  });
});

describe("pageDoorOf", () => {
  it.each<[string, Item | undefined, PageDoor | null]>([
    ["a chat", CHAT, { label: "Open chat", to: "/chat/chat_7" }],
    ["a workspace", WORKSPACE, { label: "Open workspace", to: "/workspaces/workspace_7" }],
    ["a template", TEMPLATE, { label: "Open template", to: "/templates/chat_template_7" }],
    ["a workspace with no page", folderObject("workspace", null), null],
    ["a result, which is not a folder", RESULT, null],
    ["a folder", FOLDER, null],
    ["nothing", undefined, null],
  ])("names the page of %s", (_why, item, door) => {
    expect(pageDoorOf(item)).toEqual(door);
  });
});

describe("secondPageDoorOf", () => {
  it.each<[string, Item | undefined, PageDoor | null]>([
    [
      "a workspace, whose Open lists files",
      WORKSPACE,
      { label: "Open workspace", to: "/workspaces/workspace_7" },
    ],
    ["a chat, whose Open is its page", CHAT, null],
    ["a template, whose Open is its page", TEMPLATE, null],
    ["a workspace with no page", folderObject("workspace", null), null],
    [
      "a folder named like a workspace",
      row({ kind: "folder", name: "Main.alkeraworkspace" }),
      null,
    ],
    ["a folder", FOLDER, null],
  ])("offers a second door on %s only where Open lists files", (_why, item, door) => {
    expect(secondPageDoorOf(item)).toEqual(door);
  });
});

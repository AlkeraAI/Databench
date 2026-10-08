// Where a chat's Files tab is rooted: where its agent works.

import { describe, expect, it } from "vitest";

import { chatFilesRoot, workspaceTitle } from "@/pages/workspace/workspaces/chatWorkspace";

describe("chatFilesRoot", () => {
  it.each([
    ["the workspace's shared tree first", { files_node_id: "chat", workspace_files_node_id: "ws-files" }, "work", "ws-files"],
    ["else the chat's working directory", { files_node_id: "chat", workspace_files_node_id: null }, "work", "work"],
    ["else the chat's folder", { files_node_id: "chat" }, null, "chat"],
    ["nothing for a chat with no folder", { files_node_id: null }, undefined, null],
  ])("%s", (_why, chat, working, expected) => {
    expect(chatFilesRoot(chat, working)).toBe(expected);
  });
});

describe("workspaceTitle", () => {
  it("calls a main workspace Main and an untitled one untitled", () => {
    expect(workspaceTitle({ kind: "main", title: "anything" })).toBe("Main");
    expect(workspaceTitle({ kind: "project", title: "  " })).toBe("Untitled workspace");
    expect(workspaceTitle({ kind: "project", title: "Q4" })).toBe("Q4");
  });
});

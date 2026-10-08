// How the rail sorts chats into workspaces: main first, then the reader's
// projects, then what others shared; a workspace of one is drawn as its chat.

import { describe, expect, it } from "vitest";

import type { ChatSessionRead } from "@/api/chats";
import type { WorkspaceRead } from "@/api/workspaces";
import { railGroups, railWorkspaceOf } from "@/pages/workspace/workspaces/railGroups";

const ME = "u-me";
const THEM = "u-them";

function ws(id: string, over: Partial<WorkspaceRead> = {}): WorkspaceRead {
  return {
    id,
    title: id,
    kind: "project",
    layout: "native",
    owner_user_id: ME,
    version: 1,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    chat_count: 0,
    machine_status: "none",
    writable: true,
    can_rename: true,
    can_delete: true,
    can_add_chat: true,
    ...over,
  } as WorkspaceRead;
}

function chat(id: string, workspaceId: string | null, owner = ME): ChatSessionRead {
  return {
    id,
    title: id,
    owner_user_id: owner,
    machine_id: null,
    machine_status: "ready",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    last_seq: 0,
    workspace_id: workspaceId,
  } as ChatSessionRead;
}

describe("railGroups", () => {
  it("puts main first, then the reader's projects, then shared workspaces, each holding its chats", () => {
    const groups = railGroups({
      workspaces: [
        ws("p2"),
        ws("shared-1", { owner_user_id: THEM }),
        ws("main", { kind: "main" }),
        ws("p1"),
      ],
      chats: [chat("a", "p1"), chat("b", "main"), chat("c", "shared-1", THEM), chat("d", "p1")],
      userId: ME,
      multiChat: true,
    });
    expect(groups.main?.workspace.id).toBe("main");
    expect(groups.main?.chats.map((c) => c.id)).toEqual(["b"]);
    expect(groups.projects.map((p) => p.workspace.id)).toEqual(["p2", "p1"]);
    expect(groups.projects[1]?.chats.map((c) => c.id)).toEqual(["a", "d"]);
    expect(groups.shared.map((p) => p.workspace.id)).toEqual(["shared-1"]);
    expect(groups.shared[0]?.chats.map((c) => c.id)).toEqual(["c"]);
    expect(groups.chats).toEqual([]);
    expect(groups.sharedChats).toEqual([]);
  });

  it("draws a workspace of one as its plain chat, mine and others' apart", () => {
    const groups = railGroups({
      workspaces: [
        ws("one-mine", { layout: "adopted", adopted_chat_id: "x" }),
        ws("one-theirs", { layout: "adopted", owner_user_id: THEM, adopted_chat_id: "y" }),
      ],
      chats: [chat("x", "one-mine"), chat("y", "one-theirs", THEM)],
      userId: ME,
      multiChat: false,
    });
    expect(groups.main).toBeNull();
    expect(groups.projects).toEqual([]);
    expect(groups.shared).toEqual([]);
    expect(groups.chats.map((c) => c.id)).toEqual(["x"]);
    expect(groups.sharedChats.map((c) => c.id)).toEqual(["y"]);
  });

  it("keeps a chat whose workspace this reader cannot list, as a plain chat", () => {
    const groups = railGroups({
      workspaces: [],
      chats: [chat("orphan", "w-unlisted"), chat("pre-adoption", null)],
      userId: ME,
      multiChat: true,
    });
    expect(groups.chats.map((c) => c.id)).toEqual(["orphan", "pre-adoption"]);
  });

  it("keeps every chat while the workspace list has not answered", () => {
    const groups = railGroups({
      workspaces: undefined,
      chats: [chat("a", "p1"), chat("b", "p1", THEM)],
      userId: ME,
      multiChat: true,
    });
    expect(groups.chats.map((c) => c.id)).toEqual(["a"]);
    expect(groups.sharedChats.map((c) => c.id)).toEqual(["b"]);
  });

  it.each([
    { multiChat: true, chats: [], shown: true, why: "flag on, empty" },
    { multiChat: false, chats: [], shown: false, why: "flag off, empty: it can hold nothing" },
    { multiChat: false, chats: [chat("held", "main")], shown: true, why: "flag off but it holds a chat" },
  ])("shows the main workspace: $why", ({ multiChat, chats, shown }) => {
    const groups = railGroups({ workspaces: [ws("main", { kind: "main" })], chats, userId: ME, multiChat });
    expect(groups.main !== null).toBe(shown);
  });

  it("never files somebody else's main workspace as the reader's own", () => {
    const groups = railGroups({
      workspaces: [ws("their-main", { kind: "main", owner_user_id: THEM })],
      chats: [],
      userId: ME,
      multiChat: true,
    });
    expect(groups.main).toBeNull();
    expect(groups.shared.map((s) => s.workspace.id)).toEqual(["their-main"]);
  });

  it("finds the workspace drawn around a chat, and none for a plain chat", () => {
    const groups = railGroups({
      workspaces: [ws("p1"), ws("one", { layout: "adopted" })],
      chats: [chat("in-p1", "p1"), chat("alone", "one")],
      userId: ME,
      multiChat: true,
    });
    expect(railWorkspaceOf(groups, "in-p1")?.workspace.id).toBe("p1");
    expect(railWorkspaceOf(groups, "alone")).toBeNull();
    expect(railWorkspaceOf(groups, undefined)).toBeNull();
  });
});

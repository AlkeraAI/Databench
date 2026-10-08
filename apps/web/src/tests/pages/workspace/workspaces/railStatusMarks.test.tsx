// One state reads the same on a chat row and on its workspace row: both draw
// the status the server wrote through StatusPill, so a working chat and the
// workspace it works in carry the same tone, a resting chat carries nothing,
// and each mark's tooltip is the server's sentence.

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ChatSessionRead } from "@/api/chats";
import type { StatusFact } from "@/api/status";
import type { WorkspaceRead } from "@/api/workspaces";
import {
  WorkspaceRail,
  type ChatRowHandlers,
  type WorkspaceRowActions,
} from "@/pages/workspace/workspaces/WorkspaceRail";
import type { RailGroups } from "@/pages/workspace/workspaces/railGroups";

const CHAT_WORKING: StatusFact = {
  subject: "chat",
  state: "working",
  label: "Working",
  tone: "info",
  reason_code: "",
  sentence: "The agent is working.",
};

const WORKSPACE_WORKING: StatusFact = {
  subject: "workspace",
  state: "working",
  label: "Working",
  tone: "info",
  reason_code: "",
  sentence: "An agent is working in this workspace.",
};

const CHAT_AWAKE: StatusFact = {
  subject: "chat",
  state: "awake",
  label: "Awake",
  tone: "success",
  reason_code: "",
  sentence: "Ready for a message.",
};

function chat(id: string, status: StatusFact | null): ChatSessionRead {
  return {
    id,
    title: `Chat ${id}`,
    machine_status: "ready",
    last_seq: 4,
    created_at: "2026-10-06T00:00:00Z",
    updated_at: "2026-10-06T00:00:00Z",
    unread: false,
    needs_you: false,
    status,
  } as ChatSessionRead;
}

function workspace(status: StatusFact | null): WorkspaceRead {
  return {
    id: "w1",
    title: "Pricing study",
    kind: "project",
    layout: "native",
    version: 1,
    chat_count: 2,
    unread_count: 0,
    can_rename: true,
    can_delete: true,
    can_add_chat: true,
    files_node_id: null,
    status,
  } as WorkspaceRead;
}

const handlers: ChatRowHandlers = {
  entryOf: (c) => ({ id: c.id, title: c.title ?? "", nodeId: null, owner: true, canDelete: false }),
  copyLabel: () => "Duplicate",
  onOpen: vi.fn(),
  onCopy: vi.fn(),
  onSaveAsTemplate: vi.fn(),
  onShare: vi.fn(),
  onDelete: vi.fn(),
  onRename: vi.fn(async () => undefined),
};

const actions: WorkspaceRowActions = {
  onOpen: vi.fn(),
  onNewChat: vi.fn(),
  onRename: vi.fn(async () => undefined),
  onShare: vi.fn(),
  onDelete: vi.fn(),
};

function renderRail(ws: WorkspaceRead, chats: ChatSessionRead[], working: ReadonlySet<string>): void {
  const groups: RailGroups = {
    main: null,
    projects: [{ workspace: ws, chats }],
    shared: [],
    chats: [],
    sharedChats: [],
  };
  render(
    <WorkspaceRail
      groups={groups}
      currentChatId="a"
      currentWorkspaceId="w1"
      working={working}
      empty={false}
      chat={handlers}
      workspace={actions}
    />,
  );
}

function rowOf(title: string): HTMLElement {
  const row = screen.getByText(title).closest("button");
  if (!row) throw new Error(`no row for ${title}`);
  return row;
}

describe("status marks on the rail", () => {
  it("draws a working chat and its workspace in the same tone", () => {
    // The page also knows the turn is live: that must not swap in a mark of
    // its own beside the workspace's.
    renderRail(workspace(WORKSPACE_WORKING), [chat("a", CHAT_WORKING)], new Set(["a"]));

    const chatDot = within(rowOf("Chat a")).getByRole("img", { name: "Working" });
    const workspaceDot = within(rowOf("Pricing study")).getByRole("img", { name: "Working" });
    expect(chatDot).toHaveAttribute("data-tone", "info");
    expect(workspaceDot).toHaveAttribute("data-tone", "info");
  });

  it("draws nothing for a chat the server says is resting, whatever the page tracks", () => {
    renderRail(workspace(null), [chat("a", CHAT_AWAKE), chat("b", null)], new Set(["a", "b"]));

    expect(within(rowOf("Chat a")).queryByRole("img")).toBeNull();
    expect(within(rowOf("Chat b")).queryByRole("img")).toBeNull();
  });

  it("titles each mark with the server's sentence", () => {
    renderRail(workspace(WORKSPACE_WORKING), [chat("a", CHAT_WORKING)], new Set(["a"]));

    expect(within(rowOf("Chat a")).getByRole("img", { name: "Working" })).toHaveAttribute(
      "title",
      "The agent is working.",
    );
    expect(within(rowOf("Pricing study")).getByRole("img", { name: "Working" })).toHaveAttribute(
      "title",
      "An agent is working in this workspace.",
    );
  });
});

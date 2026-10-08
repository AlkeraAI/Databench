// The rail draws each reader's read state as the server reports it: a chat
// with news is bold with a dot, a chat waiting on this reader carries its own
// mark instead, the open chat never reads unread, and a workspace row shows
// how many of its chats are unread with "Mark all as read" in its menu.

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ChatSessionRead } from "@/api/chats";
import type { WorkspaceRead } from "@/api/workspaces";
import { MARK_UNREAD } from "@/pages/workspace/chat/ChatRailRowMenu";
import { NEEDS_YOU_LABEL, UNREAD_LABEL } from "@/pages/workspace/chat/ChatRailRow";
import {
  MARK_ALL_READ,
  WorkspaceRail,
  type ChatRowHandlers,
  type WorkspaceRowActions,
} from "@/pages/workspace/workspaces/WorkspaceRail";
import type { RailGroups } from "@/pages/workspace/workspaces/railGroups";

function chat(id: string, over: Partial<ChatSessionRead> = {}): ChatSessionRead {
  return {
    id,
    title: `Chat ${id}`,
    machine_status: "ready",
    last_seq: 4,
    created_at: "2026-10-06T00:00:00Z",
    updated_at: "2026-10-06T00:00:00Z",
    unread: false,
    needs_you: false,
    ...over,
  } as ChatSessionRead;
}

function workspace(over: Partial<WorkspaceRead> = {}): WorkspaceRead {
  return {
    id: "w1",
    title: "Pricing study",
    kind: "project",
    layout: "native",
    version: 1,
    chat_count: 3,
    unread_count: 0,
    can_rename: true,
    can_delete: true,
    can_add_chat: true,
    files_node_id: null,
    ...over,
  } as WorkspaceRead;
}

function handlers(onMarkUnread = vi.fn()): ChatRowHandlers {
  return {
    entryOf: (c) => ({ id: c.id, title: c.title ?? "", nodeId: null, owner: true, canDelete: false }),
    copyLabel: () => "Duplicate",
    onOpen: vi.fn(),
    onCopy: vi.fn(),
    onSaveAsTemplate: vi.fn(),
    onShare: vi.fn(),
    onDelete: vi.fn(),
    onRename: vi.fn(async () => undefined),
    onMarkUnread,
  };
}

function actions(onMarkAllRead = vi.fn()): WorkspaceRowActions {
  return {
    onOpen: vi.fn(),
    onNewChat: vi.fn(),
    onRename: vi.fn(async () => undefined),
    onShare: vi.fn(),
    onDelete: vi.fn(),
    onMarkAllRead,
  };
}

function groups(ws: WorkspaceRead, chats: ChatSessionRead[]): RailGroups {
  return { main: null, projects: [{ workspace: ws, chats }], shared: [], chats: [], sharedChats: [] };
}

function rowOf(title: string): HTMLElement {
  const row = screen.getByText(title).closest("button");
  if (!row) throw new Error(`no row for ${title}`);
  return row;
}

describe("the rail's read state", () => {
  it("marks an unread chat, a chat that needs you, and never the open chat", () => {
    const chats = [
      chat("a", { unread: true }),
      chat("b", { needs_you: true, unread: true }),
      chat("c"),
      chat("d", { unread: true }),
    ];
    render(
      <WorkspaceRail
        groups={groups(workspace({ unread_count: 3 }), chats)}
        currentChatId="d"
        currentWorkspaceId="w1"
        working={new Set()}
        empty={false}
        chat={handlers()}
        workspace={actions()}
      />,
    );
    const a = rowOf("Chat a");
    expect(a).toHaveAttribute("data-unread");
    expect(within(a).getByRole("img", { name: UNREAD_LABEL })).toBeInTheDocument();

    const b = rowOf("Chat b");
    expect(within(b).getByRole("img", { name: NEEDS_YOU_LABEL })).toBeInTheDocument();
    expect(within(b).queryByRole("img", { name: UNREAD_LABEL })).toBeNull();

    const c = rowOf("Chat c");
    expect(c).not.toHaveAttribute("data-unread");
    expect(within(c).queryByRole("img", { name: UNREAD_LABEL })).toBeNull();

    const d = rowOf("Chat d");
    expect(d).not.toHaveAttribute("data-unread");
    expect(within(d).queryByRole("img", { name: UNREAD_LABEL })).toBeNull();

    expect(screen.getByLabelText("3 unread chats")).toHaveTextContent("3");
  });

  it("offers Mark as unread on a read chat only, and Mark all as read on a workspace with unread chats", () => {
    const onMarkUnread = vi.fn();
    const onMarkAllRead = vi.fn();
    const ws = workspace({ unread_count: 1 });
    const { rerender } = render(
      <WorkspaceRail
        groups={groups(ws, [chat("a", { unread: true }), chat("c")])}
        currentWorkspaceId="w1"
        working={new Set()}
        empty={false}
        chat={handlers(onMarkUnread)}
        workspace={actions(onMarkAllRead)}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Actions for Chat c" }));
    fireEvent.click(screen.getByRole("menuitem", { name: MARK_UNREAD }));
    expect(onMarkUnread).toHaveBeenCalledWith(expect.objectContaining({ id: "c" }));

    fireEvent.click(screen.getByRole("button", { name: "Actions for Chat a" }));
    expect(screen.queryByRole("menuitem", { name: MARK_UNREAD })).toBeNull();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

    fireEvent.click(screen.getByRole("button", { name: "Actions for Pricing study" }));
    fireEvent.click(screen.getByRole("menuitem", { name: MARK_ALL_READ }));
    expect(onMarkAllRead).toHaveBeenCalledWith(ws);

    rerender(
      <WorkspaceRail
        groups={groups(workspace({ unread_count: 0 }), [chat("c")])}
        currentWorkspaceId="w1"
        working={new Set()}
        empty={false}
        chat={handlers(onMarkUnread)}
        workspace={actions(onMarkAllRead)}
      />,
    );
    expect(screen.queryByLabelText(/unread chat/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Actions for Pricing study" }));
    expect(screen.queryByRole("menuitem", { name: MARK_ALL_READ })).toBeNull();
  });
});

// The rail beside the chat, arranged by workspace.
//
// Main first, then the reader's projects, then what others shared with them,
// each a row that opens the workspace and a chevron that unfolds its chats.
// Chats outside any workspace drawn as one (a workspace of one is just its
// chat) are listed as chats, the way the rail always listed them. With no
// workspace to draw, the rail is the plain list of chats it was before, with
// no headings over a single section.
//
// Presentation only, like the chat rail it grew from: rows in, presses out.
// Every dialog a row's action opens belongs to the page.

import { useEffect, useState, type ReactElement } from "react";

import { IconChecks, IconChevronRight, IconDotsVertical, IconFolder, IconPencil, IconPlus, IconShare, IconTrash } from "@tabler/icons-react";

import { ContextMenu, StatusPill, anchorOfElement, statusIsResting, useContextMenu, type ContextMenuItem } from "@alkera/ui";
import { safeSessionStorage } from "@alkera/ui/storage";

import type { ChatSessionRead } from "../../../api/chats";
import type { WorkspaceRead } from "../../../api/workspaces";
import { ChatRailRow, type ChatRailEntry } from "../chat/ChatRailRow";
import { RailRename } from "../chat/RailRename";

import { workspaceTitle } from "./chatWorkspace";
import type { RailGroups, RailWorkspace } from "./railGroups";

export const NEW_WORKSPACE = "New workspace";
export const OPEN_WORKSPACE = "Open";
export const RENAME_WORKSPACE = "Rename";
export const SHARE_WORKSPACE = "Share…";
export const DELETE_WORKSPACE = "Delete";
export const MARK_ALL_READ = "Mark all as read";
/** The key on a workspace's row that starts a chat in it. */
export const NEW_CHAT_IN_ROW = "Chat";
/** The share row of a chat inside a workspace that holds several chats: such a
 *  chat is shared by sharing its workspace. */
export const SHARE_CHATS_WORKSPACE = "Share workspace…";
/** How many of an open workspace's chats the rail lists before "Show more". */
export const WORKSPACE_CHATS_SHOWN = 5;
export const SHOW_MORE_CHATS = "Show more";
export const SHOW_FEWER_CHATS = "Show less";

/** The chats an open workspace lists: the first `limit` in rail order, plus
 *  any further down that the reader must not lose, in rail order: the open
 *  chat, and every chat `keep` names (one working, or waiting on an answer). */
export function shownChats(
  chats: readonly ChatSessionRead[],
  limit: number,
  currentChatId?: string,
  keep: (chat: ChatSessionRead) => boolean = () => false,
): readonly ChatSessionRead[] {
  if (chats.length <= limit) return chats;
  return chats.filter((chat, i) => i < limit || chat.id === currentChatId || keep(chat));
}

export { MAIN_WORKSPACE_TITLE, workspaceTitle } from "./chatWorkspace";

export interface WorkspaceRowActions {
  onOpen(workspace: WorkspaceRead): void;
  /** Start a chat in the workspace. Offered only where it may take one. */
  onNewChat(workspace: WorkspaceRead): void;
  /** Resolves when the new name has landed; rejects with the reason it did not. */
  onRename(workspace: WorkspaceRead, title: string): Promise<void>;
  onShare(workspace: WorkspaceRead): void;
  onDelete(workspace: WorkspaceRead): void;
  /** Mark every chat in the workspace read, for this reader. */
  onMarkAllRead?(workspace: WorkspaceRead): void;
}

export interface ChatRowHandlers {
  entryOf(chat: ChatSessionRead): ChatRailEntry;
  copyLabel(owner: boolean): string;
  onOpen(entry: ChatRailEntry): void;
  onCopy(entry: ChatRailEntry): void;
  onSaveAsTemplate(entry: ChatRailEntry): void;
  onShare(entry: ChatRailEntry): void;
  onDelete(entry: ChatRailEntry): void;
  onRename(entry: ChatRailEntry, title: string): Promise<void>;
  /** Mark the chat unread for this reader. */
  onMarkUnread?(entry: ChatRailEntry): void;
}

export interface WorkspaceRailProps {
  groups: RailGroups;
  currentChatId?: string;
  currentWorkspaceId?: string;
  /** Chats with a live turn this page knows of. They stay listed in a folded
   *  workspace; their mark is still the server's status. */
  working: ReadonlySet<string>;
  /** The chat list has answered and holds nothing. */
  empty: boolean;
  chat: ChatRowHandlers;
  workspace: WorkspaceRowActions;
  /** Make a project workspace. Absent where a workspace cannot hold chats. */
  onNewWorkspace?: () => void;
}

function workspaceItems(
  workspace: WorkspaceRead,
  actions: WorkspaceRowActions,
  startRename: () => void,
): ContextMenuItem[] {
  return [
    {
      id: "open",
      label: OPEN_WORKSPACE,
      icon: <IconFolder size={13} stroke={1.8} aria-hidden />,
      onSelect: () => actions.onOpen(workspace),
    },
    ...(actions.onMarkAllRead && (workspace.unread_count ?? 0) > 0
      ? [
          {
            id: "mark-all-read",
            label: MARK_ALL_READ,
            icon: <IconChecks size={13} stroke={1.8} aria-hidden />,
            onSelect: () => actions.onMarkAllRead?.(workspace),
          },
        ]
      : []),
    ...(workspace.can_rename && workspace.kind !== "main"
      ? [
          {
            id: "rename",
            label: RENAME_WORKSPACE,
            icon: <IconPencil size={13} stroke={1.8} aria-hidden />,
            onSelect: startRename,
          },
        ]
      : []),
    ...(workspace.files_node_id
      ? [
          {
            id: "share",
            label: SHARE_WORKSPACE,
            icon: <IconShare size={13} stroke={1.8} aria-hidden />,
            onSelect: () => actions.onShare(workspace),
          },
        ]
      : []),
    // A main workspace is never deleted, whatever the policy says of it.
    ...(workspace.can_delete && workspace.kind !== "main"
      ? [
          {
            id: "delete",
            label: DELETE_WORKSPACE,
            icon: <IconTrash size={13} stroke={1.8} aria-hidden />,
            tone: "destructive" as const,
            onSelect: () => actions.onDelete(workspace),
          },
        ]
      : []),
  ];
}

function ChatRows({
  chats,
  currentChatId,
  handlers,
  within,
}: {
  chats: readonly ChatSessionRead[];
  currentChatId?: string;
  handlers: ChatRowHandlers;
  /** The workspace these chats sit in, when it holds several. A share of one
   *  chat alone would hand its agent the workspace's whole shared tree, so the
   *  row shares the workspace instead. */
  within?: { workspace: WorkspaceRead; onShare(workspace: WorkspaceRead): void };
}): ReactElement {
  return (
    <>
      {chats.map((chat) => {
        const entry = handlers.entryOf(chat);
        // What streams into the open chat is read, so its own row is never
        // drawn unread while the reader is on it.
        const current = entry.id === currentChatId;
        const unread = !current && chat.unread === true;
        return (
          <li key={chat.id}>
            <ChatRailRow
              chatId={entry.id}
              title={entry.title}
              current={current}
              unread={unread}
              needsYou={chat.needs_you === true}
              onMarkUnread={!unread && handlers.onMarkUnread ? () => handlers.onMarkUnread?.(entry) : undefined}
              status={chat.status ?? null}
              copyLabel={handlers.copyLabel(entry.owner)}
              onOpen={() => handlers.onOpen(entry)}
              onCopy={entry.nodeId ? () => handlers.onCopy(entry) : undefined}
              onSaveAsTemplate={entry.nodeId ? () => handlers.onSaveAsTemplate(entry) : undefined}
              onShare={
                within
                  ? within.workspace.files_node_id
                    ? () => within.onShare(within.workspace)
                    : undefined
                  : entry.nodeId
                    ? () => handlers.onShare(entry)
                    : undefined
              }
              shareLabel={within ? SHARE_CHATS_WORKSPACE : undefined}
              onDelete={entry.canDelete ? () => handlers.onDelete(entry) : undefined}
              onRename={(title) => handlers.onRename(entry, title)}
            />
          </li>
        );
      })}
    </>
  );
}

export function unreadCountLabel(count: number): string {
  return `${count} unread ${count === 1 ? "chat" : "chats"}`;
}

/** How many of the workspace's chats are unread for this reader, as the
 *  server counted them. Nothing at zero. */
function UnreadCount({ count }: { count: number }): ReactElement | null {
  if (count <= 0) return null;
  const label = unreadCountLabel(count);
  return (
    <span className="ws-rail__unread" aria-label={label} title={label}>
      {count}
    </span>
  );
}

function WorkspaceRow({
  entry,
  open,
  onToggle,
  current,
  currentChatId,
  working,
  chat,
  actions,
}: {
  entry: RailWorkspace;
  open: boolean;
  onToggle(): void;
  current: boolean;
  currentChatId?: string;
  working: ReadonlySet<string>;
  chat: ChatRowHandlers;
  actions: WorkspaceRowActions;
}): ReactElement {
  const { workspace, chats } = entry;
  const menu = useContextMenu();
  const [editing, setEditing] = useState(false);
  const [reason, setReason] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const title = workspaceTitle(workspace);
  const listed = showAll
    ? chats
    : shownChats(chats, WORKSPACE_CHATS_SHOWN, currentChatId, (c) => working.has(c.id) || !statusIsResting(c.status ?? null));
  const folds = chats.length > WORKSPACE_CHATS_SHOWN;
  const label = `Actions for ${title}`;
  const listId = `ws-rail-${workspace.id}`;

  return (
    <li className="ws-rail__workspace" data-workspace-id={workspace.id}>
      {editing ? (
        <RailRename
          title={workspace.title}
          fieldLabel="Workspace title"
          onRename={(next) => actions.onRename(workspace, next)}
          onDone={() => setEditing(false)}
          onRefused={setReason}
        />
      ) : (
        <div className="chat-page__row-wrap" {...menu.triggerProps}>
          <div className="chat-page__row-line ws-rail__line">
            <button
              type="button"
              className="ws-rail__toggle"
              aria-expanded={open}
              aria-controls={listId}
              aria-label={open ? `Hide chats in ${title}` : `Show chats in ${title}`}
              onClick={onToggle}
            >
              <IconChevronRight size={14} stroke={1.8} aria-hidden className="ws-rail__chevron" data-open={open || undefined} />
            </button>
            <button
              type="button"
              className="chat-page__row ws-rail__name"
              aria-current={current ? "page" : undefined}
              data-unread={(workspace.unread_count ?? 0) > 0 ? "" : undefined}
              onClick={() => actions.onOpen(workspace)}
            >
              <span className="chat-page__row-title" title={title}>
                {title}
              </span>
              <UnreadCount count={workspace.unread_count ?? 0} />
              <StatusPill status={workspace.status} variant="dot" quiet />
            </button>
            {workspace.can_add_chat ? (
              <button
                type="button"
                className="ws-rail__new-chat"
                aria-label={`New chat in ${title}`}
                tabIndex={-1}
                onClick={() => actions.onNewChat(workspace)}
              >
                <IconPlus size={12} stroke={2} aria-hidden />
                {NEW_CHAT_IN_ROW}
              </button>
            ) : null}
            <button
              type="button"
              className="chat-page__row-menu"
              aria-label={label}
              aria-haspopup="menu"
              aria-expanded={menu.open}
              data-open={menu.open ? "" : undefined}
              // One Tab stop per row: Shift+F10 or the menu key on the row
              // opens the same menu.
              tabIndex={-1}
              onClick={(event) => menu.openAt(anchorOfElement(event.currentTarget), event.currentTarget)}
            >
              <IconDotsVertical size={14} stroke={1.8} aria-hidden />
            </button>
            <ContextMenu
              {...menu.menuProps}
              items={workspaceItems(workspace, actions, () => {
                setReason(null);
                setEditing(true);
              })}
              label={label}
            />
          </div>
        </div>
      )}
      {reason !== null ? (
        <span className="chat-page__row-error" role="alert">
          {reason}
        </span>
      ) : null}
      {open ? (
        <ul className="chat-page__list ws-rail__chats" id={listId} aria-label={`Chats in ${title}`}>
          <ChatRows
            chats={listed}
            currentChatId={currentChatId}
            handlers={chat}
            within={{ workspace, onShare: actions.onShare }}
          />
          {chats.length === 0 ? <li className="ws-rail__none">No chats yet.</li> : null}
          {folds ? (
            <li>
              <button
                type="button"
                className="ws-rail__more"
                aria-expanded={showAll}
                aria-controls={listId}
                onClick={() => setShowAll((all) => !all)}
              >
                {showAll ? SHOW_FEWER_CHATS : SHOW_MORE_CHATS}
              </button>
            </li>
          ) : null}
        </ul>
      ) : null}
    </li>
  );
}

function Section({ heading, children }: { heading: string | null; children: ReactElement | ReactElement[] }) {
  return (
    <section className="ws-rail__section" aria-label={heading ?? undefined}>
      {heading ? <h3 className="ws-rail__heading">{heading}</h3> : null}
      <ul className="chat-page__list">{children}</ul>
    </section>
  );
}

/** What the reader unfolded and folded, kept for the tab: moving between a
 *  chat and a workspace remounts the page, and a rail that forgot what it had
 *  open collapsed Main every time another workspace was opened. A per-tab
 *  convenience, so session storage, and a browser that refuses it keeps it
 *  for the page only. */
const FOLDING_KEY = "alkera.rail.folding";

interface Folding {
  unfolded: string[];
  folded: string[];
}

function readFolding(): Folding {
  try {
    const raw = safeSessionStorage().get(FOLDING_KEY);
    const parsed = raw ? (JSON.parse(raw) as Partial<Folding>) : {};
    return {
      unfolded: Array.isArray(parsed.unfolded) ? parsed.unfolded.filter((v) => typeof v === "string") : [],
      folded: Array.isArray(parsed.folded) ? parsed.folded.filter((v) => typeof v === "string") : [],
    };
  } catch {
    return { unfolded: [], folded: [] };
  }
}

function writeFolding(folding: Folding): void {
  safeSessionStorage().set(FOLDING_KEY, JSON.stringify(folding));
}

export function WorkspaceRail({
  groups,
  currentChatId,
  currentWorkspaceId,
  working,
  empty,
  chat,
  workspace,
  onNewWorkspace,
}: WorkspaceRailProps): ReactElement {
  // Which workspaces are unfolded: the one holding the open chat, or the open
  // workspace itself, always; any other once the reader unfolds it.
  const [unfolded, setUnfolded] = useState<ReadonlySet<string>>(() => new Set(readFolding().unfolded));
  const [folded, setFolded] = useState<ReadonlySet<string>>(() => new Set(readFolding().folded));
  useEffect(() => writeFolding({ unfolded: [...unfolded], folded: [...folded] }), [unfolded, folded]);
  const holding = [...(groups.main ? [groups.main] : []), ...groups.projects, ...groups.shared].find(
    (entry) => entry.chats.some((c) => c.id === currentChatId),
  );
  // A workspace the reader has been in stays unfolded when they move to
  // another: the rail remembers what it opened until they fold it.
  const visited = holding?.workspace.id ?? currentWorkspaceId;
  useEffect(() => {
    if (!visited) return;
    setUnfolded((prev) => (prev.has(visited) ? prev : new Set(prev).add(visited)));
  }, [visited]);
  const isOpen = (id: string): boolean =>
    !folded.has(id) && (unfolded.has(id) || id === holding?.workspace.id || id === currentWorkspaceId);
  const toggle = (id: string): void => {
    const open = isOpen(id);
    setUnfolded((prev) => {
      const next = new Set(prev);
      if (open) next.delete(id);
      else next.add(id);
      return next;
    });
    setFolded((prev) => {
      const next = new Set(prev);
      if (open) next.add(id);
      else next.delete(id);
      return next;
    });
  };
  const row = (entry: RailWorkspace): ReactElement => (
    <WorkspaceRow
      key={entry.workspace.id}
      entry={entry}
      open={isOpen(entry.workspace.id)}
      onToggle={() => toggle(entry.workspace.id)}
      current={entry.workspace.id === currentWorkspaceId && !currentChatId}
      currentChatId={currentChatId}
      working={working}
      chat={chat}
      actions={workspace}
    />
  );

  const anyWorkspace = Boolean(groups.main) || groups.projects.length > 0 || groups.shared.length > 0;
  const sharedRows = [
    ...groups.shared.map(row),
    ...(groups.sharedChats.length > 0
      ? [
          <ChatRows
            key="shared-chats"
            chats={groups.sharedChats}
            currentChatId={currentChatId}
            handlers={chat}
          />,
        ]
      : []),
  ];
  // Headings only where there is more than one thing to tell apart: a rail
  // that is only the reader's own chats reads as it always has.
  const headed = anyWorkspace || groups.sharedChats.length > 0;

  return (
    <nav className="chat-page__rail ws-rail" aria-label="Chats">
      {onNewWorkspace ? (
        <button type="button" className="ws-rail__new" onClick={onNewWorkspace}>
          <IconPlus size={14} stroke={1.8} aria-hidden />
          {NEW_WORKSPACE}
        </button>
      ) : null}
      {groups.main || groups.projects.length > 0 ? (
        <Section heading="Workspaces">
          {[...(groups.main ? [row(groups.main)] : []), ...groups.projects.map(row)]}
        </Section>
      ) : null}
      {groups.chats.length > 0 || (empty && !anyWorkspace) ? (
        <Section heading={headed ? "Chats" : null}>
          {[
            <ChatRows key="mine" chats={groups.chats} currentChatId={currentChatId} handlers={chat} />,
            ...(empty && !anyWorkspace
              ? [
                  <li key="empty" className="chat-page__empty">
                    No chats yet.
                  </li>,
                ]
              : []),
          ]}
        </Section>
      ) : null}
      {sharedRows.length > 0 ? <Section heading="Shared with me">{sharedRows}</Section> : null}
    </nav>
  );
}

// Everything a chat carries in the rail, in the product's one right-click menu.
//
// Two openers, one menu: a press on the row's own key, and a right-click (or
// the keyboard's menu request) anywhere on the row. Nobody needs a right mouse
// button to reach a chat's actions, and nobody has to open a chat to rename,
// copy, share or throw it away.
//
// A row an action cannot run is not drawn: a chat with no node in the drive has
// nothing to copy, share or save as a template, so those rows are absent rather
// than present and dead.

import { type ReactElement } from "react";

import {
  IconCopy,
  IconDotsVertical,
  IconMessage,
  IconMessageCircleExclamation,
  IconPencil,
  IconShare,
  IconTemplate,
  IconTrash,
} from "@tabler/icons-react";

import {
  ContextMenu,
  anchorOfElement,
  type ContextMenuItem,
  type ContextMenuState,
} from "@alkera/ui";

import { SAVE_AS_TEMPLATE } from "./useSaveAsTemplateAction";

export const OPEN_CHAT = "Open";
export const RENAME_CHAT = "Rename";
export const SHARE_CHAT = "Share…";
export const DELETE_CHAT = "Delete";
export const MARK_UNREAD = "Mark as unread";

/** What a row's menu can do. The three that need a node in the drive are
 *  optional: a chat without one offers no such row. So is Delete, which a
 *  reader who may not delete the chat is not offered. */
export interface ChatRowActions {
  onOpen: () => void;
  onRename: () => void;
  /** What the copy row says for this chat: the owner duplicates, anyone else
   *  copies into their own drive. */
  copyLabel: string;
  onCopy?: () => void;
  onSaveAsTemplate?: () => void;
  onShare?: () => void;
  /** What the share row says. A chat inside a workspace that holds several
   *  chats is shared by sharing the workspace, and the row says so. */
  shareLabel?: string;
  onDelete?: () => void;
  /** Mark the chat unread for this reader. Absent where the chat already is. */
  onMarkUnread?: () => void;
}

/** The rows, in the order a reader meets them: open it, name it, make another
 *  one out of it, let people in, throw it away. */
export function chatRowItems({
  onOpen,
  onRename,
  copyLabel,
  onCopy,
  onSaveAsTemplate,
  onShare,
  shareLabel = SHARE_CHAT,
  onDelete,
  onMarkUnread,
}: ChatRowActions): ContextMenuItem[] {
  return [
    {
      id: "open",
      label: OPEN_CHAT,
      icon: <IconMessage size={13} stroke={1.8} aria-hidden />,
      onSelect: onOpen,
    },
    {
      id: "rename",
      label: RENAME_CHAT,
      icon: <IconPencil size={13} stroke={1.8} aria-hidden />,
      onSelect: onRename,
    },
    ...(onMarkUnread
      ? [
          {
            id: "mark-unread",
            label: MARK_UNREAD,
            icon: <IconMessageCircleExclamation size={13} stroke={1.8} aria-hidden />,
            onSelect: onMarkUnread,
          },
        ]
      : []),
    ...(onCopy
      ? [
          {
            id: "copy",
            label: copyLabel,
            icon: <IconCopy size={13} stroke={1.8} aria-hidden />,
            onSelect: onCopy,
          },
        ]
      : []),
    ...(onSaveAsTemplate
      ? [
          {
            id: "save-template",
            label: SAVE_AS_TEMPLATE,
            icon: <IconTemplate size={13} stroke={1.8} aria-hidden />,
            onSelect: onSaveAsTemplate,
          },
        ]
      : []),
    ...(onShare
      ? [
          {
            id: "share",
            label: shareLabel,
            icon: <IconShare size={13} stroke={1.8} aria-hidden />,
            onSelect: onShare,
          },
        ]
      : []),
    ...(onDelete
      ? [
          {
            id: "delete",
            label: DELETE_CHAT,
            icon: <IconTrash size={13} stroke={1.8} aria-hidden />,
            tone: "destructive" as const,
            onSelect: onDelete,
          },
        ]
      : []),
  ];
}

export interface ChatRailRowMenuProps extends ChatRowActions {
  /** The chat's name, which is what the menu and its key are named after. */
  title: string;
  /** The row's menu state. It lives on the row so a right-click anywhere on the
   *  row and a press on the key below open the SAME menu. */
  menu: ContextMenuState;
}

export function ChatRailRowMenu({ title, menu, ...actions }: ChatRailRowMenuProps): ReactElement {
  const label = `Actions for ${title}`;
  return (
    <>
      <button
        type="button"
        className="chat-page__row-menu"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={menu.open}
        data-open={menu.open ? "" : undefined}
        // One Tab stop per row: the keyboard reaches the same menu from the
        // row itself (Shift+F10 or the menu key).
        tabIndex={-1}
        // Anchored under the key, the way a right-click anchors under the
        // pointer, and the key takes focus back when the menu closes.
        onClick={(event) => menu.openAt(anchorOfElement(event.currentTarget), event.currentTarget)}
      >
        <IconDotsVertical size={14} stroke={1.8} aria-hidden />
      </button>
      <ContextMenu {...menu.menuProps} items={chatRowItems(actions)} label={label} />
    </>
  );
}

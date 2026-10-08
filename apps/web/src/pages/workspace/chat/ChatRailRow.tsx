// One chat in the rail: its name, its status mark, and the key that opens
// everything that can be done to it.
//
// The status mark is the server's status for the chat, drawn by the same
// StatusPill and tone table as its workspace's row, so one state never reads
// in two colours. A resting chat (awake, asleep) draws nothing.
//
// The actions live in the product's one right-click menu, opened the three ways
// a reader looks for them — a press on the row's own key, a right-click
// anywhere on the row, and the keyboard's own menu request (Shift+F10, or the
// menu key) on the focused row. The key is quiet until the row is under the
// pointer or holds focus, so a rail at rest still reads as a column of names,
// and it is laid over the row's right end rather than beside it so a rail at
// rest is also a column of evenly inset rectangles.
//
// The rail is narrow and chat names are not, so most names break off. The name
// carries itself as its own tip: resting on the row spells it out in full.
//
// Renaming happens in place. A refusal is read out under the row in the
// server's own words rather than the name silently springing back.

import { useState, type ReactElement } from "react";

import { IconHandStop } from "@tabler/icons-react";

import { StatusPill, useContextMenu } from "@alkera/ui";

import { ChatRailRowMenu, type ChatRowActions } from "./ChatRailRowMenu";
import { RailRename } from "./RailRename";
import type { StatusFact } from "../../../api/status";

/** One chat, as the rail needs it. `nodeId` is the chat's folder in the drive:
 *  `null` for a chat that has none, which is what withholds the three actions
 *  that act on a folder rather than on the conversation. */
export interface ChatRailEntry {
  id: string;
  title: string;
  nodeId: string | null;
  /** Whether this reader owns the chat. It only decides what the copy is
   *  CALLED, which differs for a chat somebody else owns. */
  owner: boolean;
  /** Whether this reader may delete the chat, as the listing decided it. A
   *  row they may not delete offers no Delete. */
  canDelete: boolean;
}

export interface ChatRailRowProps extends Omit<ChatRowActions, "onRename"> {
  chatId: string;
  title: string;
  /** Whether this is the chat the page is showing. */
  current: boolean;
  /** The chat's status as the server wrote it. */
  status?: StatusFact | null;
  /** Resolves when the new name has landed; rejects with the reason it did
   *  not, which the row reads out. */
  onRename: (title: string) => Promise<void>;
  /** The server's word that the chat has news for this reader. */
  unread?: boolean;
  /** The server's word that the agent is waiting on an answer this reader may
   *  give. Drawn in place of the unread dot. */
  needsYou?: boolean;
}

export const UNREAD_LABEL = "Unread";
export const NEEDS_YOU_LABEL = "Needs your answer";

/** The row's read mark: what the server said, nothing worked out here. */
function ReadMark({ unread, needsYou }: { unread: boolean; needsYou: boolean }): ReactElement | null {
  if (needsYou) {
    return (
      <span className="chat-page__row-needs" role="img" aria-label={NEEDS_YOU_LABEL} title={NEEDS_YOU_LABEL}>
        <IconHandStop size={12} stroke={2} aria-hidden />
      </span>
    );
  }
  if (!unread) return null;
  return <span className="chat-page__row-unread" role="img" aria-label={UNREAD_LABEL} title={UNREAD_LABEL} />;
}

export function ChatRailRow({
  chatId,
  title,
  current,
  status,
  copyLabel,
  onCopy,
  onSaveAsTemplate,
  onShare,
  shareLabel,
  onOpen,
  onDelete,
  onRename,
  onMarkUnread,
  unread = false,
  needsYou = false,
}: ChatRailRowProps): ReactElement {
  const menu = useContextMenu();
  const [editing, setEditing] = useState(false);
  const [reason, setReason] = useState<string | null>(null);

  if (editing) {
    return (
      <RailRename
        title={title}
        onRename={onRename}
        onDone={() => setEditing(false)}
        onRefused={setReason}
      />
    );
  }

  return (
    <div className="chat-page__row-wrap" {...menu.triggerProps}>
      <div className="chat-page__row-line">
        <button
          type="button"
          className="chat-page__row"
          aria-current={current ? "page" : undefined}
          data-unread={unread || needsYou ? "" : undefined}
          onClick={onOpen}
        >
          <ReadMark unread={unread} needsYou={needsYou} />
          <span className="chat-page__row-title" title={title}>
            {title}
          </span>
          <StatusPill status={status} variant="dot" quiet />
        </button>
        <ChatRailRowMenu
          title={title}
          menu={menu}
          onOpen={onOpen}
          onRename={() => {
            setReason(null);
            setEditing(true);
          }}
          copyLabel={copyLabel}
          onCopy={onCopy}
          onSaveAsTemplate={onSaveAsTemplate}
          onShare={onShare}
          shareLabel={shareLabel}
          onDelete={onDelete}
          onMarkUnread={onMarkUnread}
        />
      </div>
      {reason !== null ? (
        <span className="chat-page__row-error" role="alert" data-chat-id={chatId}>
          {reason}
        </span>
      ) : null}
    </div>
  );
}

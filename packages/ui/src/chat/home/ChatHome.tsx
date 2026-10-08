// The chat home: the chrome, the chats, and the dock that starts a new one. The
// shell is the transcript panel's own (header, scrolling body, dock), so moving
// between home and a chat never moves the furniture.

import type { ReactElement, ReactNode } from "react";

import { ChatRow, type ChatRowView } from "./ChatRow";
import "../panel/panel.css";
import "./home.css";

export interface ChatHomeProps {
  /** The order is the caller's: the list renders what it is given. */
  chats: readonly ChatRowView[];
  header?: ReactNode;
  /** The new-chat composer. */
  dock?: ReactNode;
  /** Stands in for the list while there are no chats. */
  empty?: ReactNode;
  onOpen: (id: string) => void;
  /** A row's menu carries only the actions the host wired. */
  onOpenInEditor?: (id: string) => void;
  onDelete?: (id: string) => void;
}

export function ChatHome({
  chats,
  header,
  dock,
  empty,
  onOpen,
  onOpenInEditor,
  onDelete,
}: ChatHomeProps): ReactElement {
  const body =
    chats.length > 0 ? (
      <ul className="chat-home">
        {chats.map((chat) => (
          <ChatRow
            key={chat.id}
            row={chat}
            onOpen={onOpen}
            onOpenInEditor={onOpenInEditor}
            onDelete={onDelete}
          />
        ))}
      </ul>
    ) : empty ? (
      <div className="chat-home__empty">{empty}</div>
    ) : null;
  return (
    <div className="chat-panel">
      {header}
      <div className="chat-scroll">
        {body}
      </div>
      {dock ? (
        <div className="chat-dock">
          <div className="chat-dock__inner">{dock}</div>
        </div>
      ) : null}
    </div>
  );
}

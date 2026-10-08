// A workspace's own page: its chats, its files, and who is in it now.
//
// It sits where a chat's transcript sits, with the rail beside it, so moving
// between a workspace and one of its chats keeps the rest of the screen where
// it was. Presentation over props, except the file listing, which reads the
// workspace's tree through the drive's own hooks.

import { useState, type ReactElement, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { Avatar, Button, StatusPill, Tooltip, statusIsResting } from "@alkera/ui";

import type { ChatSessionRead } from "../../../api/chats";
import { faceTitle, initialsOf } from "../../../api/realtime/presence";
import type { WorkspaceRead } from "../../../api/workspaces";
import { hueOf } from "../../../lib/personHue";
import { RailRename } from "../chat/RailRename";

import { chatTitle } from "@/lib/chatTitle";
import { ownedBy } from "./railGroups";
import type { WorkspaceViewer } from "./useWorkspacePresence";
import { workspaceTitle } from "./WorkspaceRail";

export const NEW_CHAT_IN_WORKSPACE = "New chat";
/** One spelling of an empty list of chats, here and in the rail. */
export const NO_CHATS = "No chats yet.";
export const OPEN_FILES = "Open Files";
/** Where someone is when they have the workspace open and none of its chats. */
export const WORKSPACE_PAGE = "Workspace page";
export const AGENT_WORKING = "Agent working";

export interface WorkspaceOverviewProps {
  workspace: WorkspaceRead;
  chats: readonly ChatSessionRead[];
  viewers: readonly WorkspaceViewer[];
  selfUserId: string | null;
  onNewChat?: () => void;
  onOpenChat(chatId: string): void;
  onRename?: (title: string) => Promise<void>;
  onShare?: () => void;
  onDelete?: () => void;
  /** Where the workspace runs: its machine chip. */
  machine?: ReactNode;
}

function Faces({
  viewers,
  chats,
  selfUserId,
}: {
  viewers: readonly WorkspaceViewer[];
  chats: readonly ChatSessionRead[];
  selfUserId: string | null;
}): ReactElement {
  const titleOf = (id: string): string => chatTitle(chats.find((c) => c.id === id) ?? {});
  return (
    <ul className="ws-page__people" aria-label="People here now">
      {viewers.map((viewer) => {
        const name = faceTitle(viewer, selfUserId);
        const where = viewer.chatIds.length > 0 ? viewer.chatIds.map(titleOf).join(", ") : WORKSPACE_PAGE;
        return (
          <li key={viewer.userId} className="ws-page__person" data-user-id={viewer.userId}>
            <Tooltip label={`${name} · ${where}`} side="bottom" wrap>
              {(tip) => (
                <Avatar
                  {...tip}
                  className="ws-page__face"
                  label={name}
                  tabIndex={0}
                  hue={hueOf(viewer)}
                  picture={viewer.avatarUrl}
                  initials={initialsOf(viewer.name || viewer.email)}
                />
              )}
            </Tooltip>
            <span className="ws-page__person-name">{name}</span>
            <span className="ws-page__person-where">{where}</span>
          </li>
        );
      })}
    </ul>
  );
}

export function WorkspaceOverview({
  workspace,
  chats,
  viewers,
  selfUserId,
  onNewChat,
  onOpenChat,
  onRename,
  onShare,
  onDelete,
  machine,
}: WorkspaceOverviewProps): ReactElement {
  const [editing, setEditing] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const title = workspaceTitle(workspace);
  // The agents here now are the chats the server says have a turn running.
  const agents = chats.filter((chat) => chat.status?.state === "working");
  // The workspace's own folder in Files: its working tree, else its folder.
  const filesFolder = workspace.working_node_id ?? workspace.files_node_id ?? null;
  const shared = !ownedBy(workspace, selfUserId);

  return (
    <div className="ws-page" data-workspace-id={workspace.id}>
      <header className="ws-page__head">
        <div className="ws-page__title-line">
          {editing && onRename ? (
            <RailRename
              title={workspace.title}
              fieldLabel="Workspace title"
              onRename={onRename}
              onDone={() => setEditing(false)}
              onRefused={setRefusal}
            />
          ) : (
            <h1 className="ws-page__title">{title}</h1>
          )}
          <StatusPill status={workspace.status} />
          {machine}
          {shared ? <span className="ws-page__tag">Shared with you</span> : null}
        </div>
        <div className="ws-page__actions">
          {onNewChat ? (
            <Button onClick={onNewChat}>{NEW_CHAT_IN_WORKSPACE}</Button>
          ) : null}
          {onRename ? (
            <Button
              variant="secondary"
              fill="outline"
              onClick={() => {
                setRefusal(null);
                setEditing(true);
              }}
            >
              Rename
            </Button>
          ) : null}
          {onShare ? (
            <Button variant="secondary" fill="outline" onClick={onShare}>
              Share
            </Button>
          ) : null}
          {onDelete ? (
            <Button variant="destructive" fill="ghost" onClick={onDelete}>
              Delete
            </Button>
          ) : null}
        </div>
        {refusal ? (
          <p className="ws-page__refusal" role="alert">
            {refusal}
          </p>
        ) : null}
      </header>

      {/* Only when somebody else is here: on a workspace nobody else is in, a
          section that says so is filler, and on a slow link it was the first
          paint before the roster had answered. */}
      {viewers.length > 0 || agents.length > 0 ? (
      <section className="ws-page__section" aria-labelledby="ws-page-people">
        <h2 id="ws-page-people" className="ws-page__heading">
          Here now
        </h2>
        {viewers.length > 0 ? <Faces viewers={viewers} chats={chats} selfUserId={selfUserId} /> : null}
        {agents.length > 0 ? (
          <ul className="ws-page__agents" aria-label="Agents">
            {agents.map((chat) => (
              <li key={chat.id} className="ws-page__agent">
                <StatusPill status={chat.status} variant="dot" />
                <span>{AGENT_WORKING}</span>
                <span className="ws-page__person-where">{chatTitle(chat)}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </section>
      ) : null}

      <section className="ws-page__section" aria-labelledby="ws-page-chats">
        <h2 id="ws-page-chats" className="ws-page__heading">
          Chats
        </h2>
        {chats.length === 0 ? (
          <p className="ws-page__muted">{NO_CHATS}</p>
        ) : (
          <ul className="ws-page__chats">
            {chats.map((chat) => (
              <li key={chat.id}>
                <button type="button" className="ws-page__chat" onClick={() => onOpenChat(chat.id)}>
                  <span className="ws-page__chat-title">{chatTitle(chat)}</span>
                  {/* A healthy or resting chat says nothing in a list row. */}
                  {statusIsResting(chat.status) ? null : <StatusPill status={chat.status} />}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {filesFolder ? (
        <Link
          className="alk-btn ws-page__open-files"
          data-variant="secondary"
          data-fill="outline"
          data-size="md"
          to={`/files/${filesFolder}`}
        >
          {OPEN_FILES}
        </Link>
      ) : null}
    </div>
  );
}

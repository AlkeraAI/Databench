// The page behind `/workspaces/:workspaceId`, drawn where a chat's transcript
// is drawn so the rail stays put between a workspace and its chats. It reads
// the workspace, says plainly when there is none to show, and otherwise hands
// the overview its chats, its people and what the reader may do.

import { useMemo, type ReactElement } from "react";
import { Link, useNavigate } from "react-router-dom";

import { EmptyState } from "@alkera/ui";

import { sessionIsUp, useWakeWorkspaceOnOpen, type ChatSessionRead } from "../../../api/chats";
import { ApiError } from "../../../api/errors";
import { useWorkspace, type WorkspaceRead } from "../../../api/workspaces";
import { useSpecificTitle } from "../../../app/documentTitle";

import { workspaceTitle } from "./chatWorkspace";
import type { WorkspaceViewer } from "./useWorkspacePresence";
import type { WorkspaceRailState } from "./useWorkspaceRail";
import { WorkspaceOverview } from "./WorkspaceOverview";
import { WakeMachinePrompt, WorkspaceMachineChip } from "./WorkspaceMachine";

export const WORKSPACE_GONE_TITLE = "This workspace doesn't exist";
export const WORKSPACE_GONE_BODY = "It was deleted, or it was never shared with you.";

export interface WorkspacePageProps {
  workspaceId: string;
  rail: WorkspaceRailState;
  /** Every chat the reader may list; the page shows the ones in this workspace. */
  chats: readonly ChatSessionRead[];
  viewers: readonly WorkspaceViewer[];
  userId: string | null;
  onShare(workspace: WorkspaceRead): void;
  /** Where the dead end's one key leads. */
  newChatPath: string;
  newChatLabel: string;
}

export function WorkspacePage({
  workspaceId,
  rail,
  chats,
  viewers,
  userId,
  onShare,
  newChatPath,
  newChatLabel,
}: WorkspacePageProps): ReactElement | null {
  const navigate = useNavigate();
  // The list already carries every workspace this reader may open, and keeps
  // it current; the workspace's own read is asked only when the list does not
  // have it (still loading it, or an address to a workspace that is not
  // there, whose answer is the dead end below).
  const listed = rail.listed?.find((w) => w.id === workspaceId) ?? null;
  const read = useWorkspace(rail.listed && !listed ? workspaceId : undefined);
  const known = listed ?? read.data ?? null;
  useSpecificTitle(known ? workspaceTitle(known) : null);
  const own = useMemo(
    () => chats.filter((row) => row.workspace_id === workspaceId),
    [chats, workspaceId],
  );
  // Opening the workspace wakes it, so its notebooks run before a chat is
  // opened. The server picks the chat; nothing is asked for a workspace this
  // reader does not have, or one that already reads as up.
  const up = own.some((row) => sessionIsUp(row.status?.state));
  const opened = useWakeWorkspaceOnOpen(known ? workspaceId : undefined, up);
  // A 404 is the server's answer that there is nothing here for this reader:
  // deleted, or never shared, and the two are one opaque answer by design.
  const refusal = read.error ?? read.failureReason;
  if (refusal instanceof ApiError && refusal.status === 404) {
    return (
      <EmptyState
        title={WORKSPACE_GONE_TITLE}
        body={WORKSPACE_GONE_BODY}
        action={
          <Link className="alk-btn" to={newChatPath}>
            {newChatLabel}
          </Link>
        }
      />
    );
  }
  // The listing already knows the workspace, so it is drawn while its own
  // read is on the wire.
  const workspace = known;
  if (!workspace) return null;
  const changeable = workspace.kind !== "main";
  return (
    <>
      <WakeMachinePrompt held={opened.unavailable} onWake={opened.wake} />
      <WorkspaceOverview
        workspace={workspace}
        chats={own}
        viewers={viewers}
        selfUserId={userId}
        onNewChat={workspace.can_add_chat ? () => rail.startIn(workspace) : undefined}
        onOpenChat={(id) => navigate(`/chat/${id}`)}
        onRename={
          changeable && workspace.can_rename ? (title) => rail.rename(workspace, title) : undefined
        }
        onShare={workspace.files_node_id ? () => onShare(workspace) : undefined}
        onDelete={changeable && workspace.can_delete ? () => rail.askDelete(workspace) : undefined}
        machine={
          <WorkspaceMachineChip workspaceId={workspace.id} workspaceVersion={workspace.version} />
        }
      />
    </>
  );
}

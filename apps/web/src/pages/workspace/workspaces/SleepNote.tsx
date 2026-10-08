// The line that says where a new chat will be made.

import type { ReactElement } from "react";

import type { WorkspaceRead } from "../../../api/workspaces";

import { workspaceTitle } from "./chatWorkspace";

/** What a link to a new chat in a workspace this reader cannot use says. */
export const NEW_CHAT_REFUSED = "You don't have access to that workspace.";

export function NewChatRefused(): ReactElement {
  return (
    <p className="ws-new-chat-place" role="status">
      {NEW_CHAT_REFUSED}
    </p>
  );
}

/** Where the empty composer's chat will be made. */
export function NewChatPlace({ workspace }: { workspace: Pick<WorkspaceRead, "kind" | "title"> }): ReactElement {
  return <p className="ws-new-chat-place">In {workspaceTitle(workspace)}</p>;
}

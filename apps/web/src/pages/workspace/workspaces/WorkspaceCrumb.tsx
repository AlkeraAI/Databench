// Where the open chat sits: its workspace and a key back to it. Drawn only for
// a workspace made as one; a chat that is its own workspace has nothing above
// it. The workspace's other chats are in the rail, not here.

import type { ReactElement } from "react";
import { Link } from "react-router-dom";

import { StatusPill } from "@alkera/ui";

import { workspaceTitle } from "./chatWorkspace";
import type { RailWorkspace } from "./railGroups";

export interface WorkspaceCrumbProps {
  holder: RailWorkspace;
}

export function WorkspaceCrumb({ holder }: WorkspaceCrumbProps): ReactElement {
  const { workspace } = holder;
  return (
    <nav className="ws-crumb" aria-label="Workspace">
      <Link className="ws-crumb__workspace" to={`/workspaces/${workspace.id}`}>
        {workspaceTitle(workspace)}
      </Link>
      <StatusPill status={workspace.status} variant="dot" quiet />
    </nav>
  );
}

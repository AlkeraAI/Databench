// Everything the chat page does with workspaces, in one place: which chats sit
// together, the workspace on screen, and the writes (a chat started in a
// workspace, a project made, renamed or ended) with the dialogs that ask for
// them. The page draws; this decides.

import { useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { ConfirmDialog } from "@alkera/ui";

import { useCurrentMachine, type ChatSessionRead } from "../../../api/chats";
import { usePublicConfig } from "../../../api/config";
import { useFrames } from "../../../api/events/frameBus";
import { createRefreshThrottle, MACHINE_RATE_NODE_REASONS } from "../../../api/events/machineRefresh";
import { ApiError, refusalCopy } from "../../../api/errors";
import { keys } from "../../../api/keys";
import { useOrgMachines } from "../../../api/machines";
import { usePermissions, type GrantList } from "../../../api/files";
import {
  useCreateWorkspace,
  useDeleteWorkspace,
  useMainWorkspace,
  useRenameWorkspace,
  useWorkspaces,
  type WorkspaceRead,
} from "../../../api/workspaces";

import { workspaceTitle } from "./chatWorkspace";
import { NewWorkspaceDialog } from "./NewWorkspaceDialog";
import { ownedBy, railGroups, railWorkspaceOf, type RailGroups, type RailWorkspace } from "./railGroups";
import { RenameRefusedError } from "../chat/RailRename";

const CONFLICT = "This workspace changed since you opened it; try again.";

/** The refusal a create answers when the person already owns as many project
 *  workspaces as their org allows; the server's sentence names the limit. */
const PROJECT_CAP_REACHED = "workspace_project_cap_reached";

function createRefusal(error: unknown): string {
  if (error instanceof ApiError && error.code === PROJECT_CAP_REACHED) {
    return error.serverMessage ?? "You've reached the limit of project workspaces.";
  }
  return refusalCopy(error, { conflict: CONFLICT, fallback: "The workspace could not be made." });
}

/** The shortest gap between two re-reads of the workspace list on node frames. */
export const WORKSPACES_REFRESH_MS = 10_000;

/** The empty composer's query parameter naming the workspace its chat is made in. */
export const NEW_CHAT_WORKSPACE_PARAM = "workspace";

/** Who besides the owner a workspace is shared with, as the delete dialog
 *  says it ("Shared with 2 people and 1 team."), or null when nobody is: the
 *  people named here lose it too, and the dialog is the last place to learn
 *  that. The owner's own grant (a grant to the owner's user) is not a share. */
export function sharedWithLine(
  grants: GrantList["value"] | undefined,
  ownerUserId: string | null | undefined,
): string | null {
  let people = 0;
  let teams = 0;
  for (const grant of grants ?? []) {
    if (grant.principal.kind === "user") {
      if (grant.principal.id !== ownerUserId) people += 1;
    } else if (grant.principal.kind === "team") teams += 1;
  }
  const parts = [
    people > 0 ? `${people} ${people === 1 ? "person" : "people"}` : null,
    teams > 0 ? `${teams} ${teams === 1 ? "team" : "teams"}` : null,
  ].filter((part): part is string => part !== null);
  return parts.length === 0 ? null : `Shared with ${parts.join(" and ")}.`;
}

export interface WorkspaceRailInput {
  chats: readonly ChatSessionRead[];
  userId: string | null;
  chatId: string | undefined;
  workspaceId: string | undefined;
  /** The empty composer's path: where a new chat is written, and where the
   *  page goes when the workspace it shows is ended. */
  newChatPath: string;
}

export interface WorkspaceRailState {
  /** Whether a workspace may hold several chats on this server. */
  multiChat: boolean;
  groups: RailGroups;
  /** Every workspace the rail knows, the main one included before the list
   *  catches up with it. */
  listed: readonly WorkspaceRead[] | undefined;
  /** The workspace drawn around the open chat, or `null` for a chat that is
   *  its own workspace. */
  holder: RailWorkspace | null;
  /** Open the empty composer for a chat in `workspace`. Nothing is made until
   *  the first message is sent, so an abandoned "New chat" leaves no empty
   *  chat in anybody's rail. */
  startIn(workspace: WorkspaceRead): void;
  /** Resolves when the new name landed; rejects with the reason it did not. */
  rename(workspace: WorkspaceRead, title: string): Promise<void>;
  askNew(): void;
  askDelete(workspace: WorkspaceRead): void;
  /** The naming and deleting dialogs, for the page to mount once. */
  dialogs: ReactElement;
}

/** Keep the workspace list in step with the chat list.
 *
 *  A workspace is shared by sharing its folder, and that grant reaches the
 *  other person as a node frame, the same frame a box sends for every file it
 *  saves; re-reading the workspace list on each of those cost an open page
 *  dozens of requests a minute. The chat list already re-reads on them, and a
 *  share shows there first (the workspace's chats appear), so the workspace
 *  list is re-read only when the two disagree: a chat in a workspace the list
 *  does not have (just shared), or a workspace someone else owns with no chat
 *  left in the list (just unshared). Once per disagreement, so a workspace the
 *  server will not list does not become a loop. */
function useWorkspacesFollowChats(
  chats: readonly ChatSessionRead[],
  listed: readonly WorkspaceRead[] | undefined,
  userId: string | null,
): void {
  const queryClient = useQueryClient();
  const asked = useRef(new Set<string>());
  useEffect(() => {
    if (!listed || userId === null) return;
    const known = new Set(listed.map((w) => w.id));
    const holding = new Set(chats.map((c) => c.workspace_id).filter((id): id is string => Boolean(id)));
    const missing = [...holding].filter((id) => !known.has(id));
    const emptied = listed
      .filter((w) => !ownedBy(w, userId) && w.layout === "native" && !holding.has(w.id))
      .map((w) => w.id);
    const fresh = [...missing, ...emptied].filter((id) => !asked.current.has(id));
    if (fresh.length === 0) return;
    for (const id of fresh) asked.current.add(id);
    void queryClient.invalidateQueries({ queryKey: keys.workspaces.all });
  }, [chats, listed, userId, queryClient]);
}

export function useWorkspaceRail({
  chats,
  userId,
  chatId,
  workspaceId,
  newChatPath,
}: WorkspaceRailInput): WorkspaceRailState {
  const navigate = useNavigate();
  const multiChat = usePublicConfig().data?.workspaces_multi_chat === true;
  const workspaces = useWorkspaces();
  // The main workspace is made on first ask, so it is asked for here: a reader
  // who never started a chat in it still sees where one would land. Until the
  // list carries it (its creation reaches the list as an event), it is added
  // from its own read.
  const main = useMainWorkspace({ enabled: multiChat });
  const listed = useMemo(() => {
    const rows = workspaces.data;
    const own = main.data;
    if (!rows || !own || rows.some((w) => w.id === own.id)) return rows;
    return [own, ...rows];
  }, [workspaces.data, main.data]);
  const groups = useMemo(
    () => railGroups({ workspaces: listed, chats, userId, multiChat }),
    [listed, chats, userId, multiChat],
  );
  const holder = railWorkspaceOf(groups, chatId);
  useWorkspacesFollowChats(chats, listed, userId);
  // A share of a workspace's folder, made or revoked, reaches this reader as a
  // node frame a person caused. The chat list is not enough to see it (a
  // shared workspace may hold no chat yet, and a revoked collaborator keeps
  // the chat they started), so those frames re-read the workspace list too,
  // at most once per window: a box's saves carry their own reasons and never
  // count, and the rest are a person's pace.
  const queryClient = useQueryClient();
  const [listRefresh] = useState(() => createRefreshThrottle(queryClient, { windowMs: WORKSPACES_REFRESH_MS }));
  useEffect(() => () => listRefresh.dispose(), [listRefresh]);
  useFrames(
    (frame) =>
      frame.type === "file_node.changed" &&
      (frame.reason === undefined || !MACHINE_RATE_NODE_REASONS.has(frame.reason)),
    () => listRefresh.request(keys.workspaces.all),
  );

  const createWorkspace = useCreateWorkspace();
  const renameWorkspace = useRenameWorkspace();
  // Leaving happens the moment the server agreed, before the cache re-reads
  // the chats the delete just ended, and never on a refusal.
  const deleteWorkspace = useDeleteWorkspace((id) => {
    if (id === workspaceId || id === holder?.workspace.id) navigate(newChatPath, { replace: true });
  });
  // The client id is minted when the dialog opens, so a second press of
  // Create (or a retry after a network failure) lands on the same workspace.
  const [naming, setNaming] = useState<{ clientId: string; error: string | null } | null>(null);
  // The machines a new workspace may be pinned to: the org's machines this reader may use. Read
  // only while the dialog is open.
  const usable = useOrgMachines(naming !== null);
  const runOn = useMemo(
    () =>
      (usable.data ?? [])
        .filter((m) => m.can_use && m.use_mode === "assigned")
        .map((m) => ({ id: m.id, card: m.card })),
    [usable.data],
  );
  const runOnDefault = (usable.data ?? []).some((m) => m.use_mode === "pool") ? "Org machines" : "Standard";
  const orgDefaultId = (usable.data ?? []).find((m) => m.org_default)?.id ?? null;
  // Whether the default placement serves the org's regular chats: the server's answer for a
  // chat with no workspace, read while the dialog is open.
  const placement = useCurrentMachine(null, { enabled: naming !== null });
  const defaultAvailable = placement.data?.status !== "none";
  const [ending, setEnding] = useState<{ workspace: WorkspaceRead; error: string | null } | null>(
    null,
  );

  const startIn = (workspace: WorkspaceRead): void => {
    navigate(`${newChatPath}?${NEW_CHAT_WORKSPACE_PARAM}=${encodeURIComponent(workspace.id)}`);
  };

  const rename = async (workspace: WorkspaceRead, title: string): Promise<void> => {
    try {
      await renameWorkspace.mutateAsync({
        workspaceId: workspace.id,
        title,
        expectedVersion: workspace.version,
      });
    } catch (error) {
      throw new RenameRefusedError(
        refusalCopy(error, { conflict: CONFLICT, fallback: "This workspace could not be renamed." }),
      );
    }
  };

  const make = (title: string, machinePin: string | null | undefined): void => {
    if (!naming) return;
    createWorkspace.mutate(
      { title, clientId: naming.clientId, machinePin },
      {
        onSuccess: (workspace) => {
          setNaming(null);
          navigate(`/workspaces/${workspace.id}`);
        },
        onError: (error) =>
          setNaming((held) =>
            held
              ? {
                  ...held,
                  error: createRefusal(error),
                }
              : held,
          ),
      },
    );
  };

  // Read only while the delete is being asked about.
  const ended = ending?.workspace;
  const grants = usePermissions(
    ended?.files_drive_id ?? undefined,
    ended?.files_node_id ?? undefined,
  );
  const shared = ended ? sharedWithLine(grants.data?.value, ended.owner_user_id) : null;

  const end = (workspace: WorkspaceRead): void => {
    deleteWorkspace.mutate(workspace.id, {
      onSuccess: () => setEnding(null),
      onError: (error) =>
        setEnding({
          workspace,
          error: refusalCopy(error, {
            conflict: CONFLICT,
            forbidden: "You cannot delete this workspace.",
            fallback: "The workspace could not be deleted.",
          }),
        }),
    });
  };

  const dialogs = (
    <>
      <NewWorkspaceDialog
        open={naming !== null}
        busy={createWorkspace.isPending}
        error={naming?.error ?? null}
        onClose={() => setNaming(null)}
        onCreate={make}
        machines={runOn}
        defaultLabel={runOnDefault}
        orgDefaultId={orgDefaultId}
        defaultAvailable={defaultAvailable}
      />
      <ConfirmDialog
        open={ending !== null}
        onClose={() => setEnding(null)}
        onConfirm={() => (ending ? end(ending.workspace) : undefined)}
        title={ending ? `Delete ${workspaceTitle(ending.workspace)}?` : "Delete this workspace?"}
        consequence={
          shared ? `${shared} Its chats and files move to Trash.` : "Its chats and files move to Trash."
        }
        confirmLabel="Delete"
        tone="destructive"
        busy={deleteWorkspace.isPending}
      >
        {ending?.error ? (
          <p className="ws-dialog__refusal" role="alert">
            {ending.error}
          </p>
        ) : null}
      </ConfirmDialog>
    </>
  );

  return {
    multiChat,
    groups,
    listed,
    holder,
    startIn,
    rename,
    askNew: () => setNaming({ clientId: crypto.randomUUID(), error: null }),
    askDelete: (workspace) => setEnding({ workspace, error: null }),
    dialogs,
  };
}

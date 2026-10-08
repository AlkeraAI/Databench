// Who is in a workspace now.
//
// Two rosters make the answer. The workspace's own presence channel
// (`doc:workspace:<id>`) holds everyone with the workspace open: its page, or
// any chat inside it, since both join it. And each chat's roster, watched
// without joining (`observePresence`), says which chat a person is in, and
// keeps counting a reader whose build predates the workspace channel and
// joins only the chat. Watching a chat is not having it open, so this reader's
// face never lands on a chat they are not in.
//
// One face per person however many chats and tabs they have, the way a chat's
// own stack counts, with the chats they are in kept beside them. A server from
// before the workspace channel refuses the subscribe; the chat rosters still
// answer.

import { useEffect, useMemo, useState } from "react";

import {
  observePresence,
  usePresence,
  useRealtimePeer,
  viewersAcross,
  type PresencePeer,
  type PresenceViewer,
} from "../../../api/realtime/presence";
import { channelOf } from "../../../api/realtime/wsClient";

/** How many of a workspace's chats are watched at once. The socket holds a
 *  bounded number of channels and the chat page needs some of its own, so the
 *  most recent chats are watched and the rest are not. */
export const MAX_WATCHED_CHATS = 12;

/** The workspace's presence channel. Not a document: no envelope names it, so
 *  it is spelled here rather than through the document grammar. */
export function workspaceChannel(workspaceId: string): string {
  return `doc:workspace:${workspaceId}`;
}

/** The key the workspace's own roster is filed under beside the chats'. */
export const WORKSPACE_ROSTER = "workspace";

export interface WorkspaceViewer extends PresenceViewer {
  /** The chats in this workspace they have open; empty for someone on the
   *  workspace's own page and in none of its chats. */
  chatIds: string[];
}

/** The people behind several rosters, one entry per person, without this tab's
 *  own socket. Pure, so the union can be pinned without a socket. The
 *  workspace's own roster is filed under {@link WORKSPACE_ROSTER} and adds a
 *  person without adding a chat. */
export function workspaceViewersOf(
  rosters: ReadonlyMap<string, readonly PresencePeer[]>,
  selfPeerId: string | null,
): WorkspaceViewer[] {
  const placed = [...rosters].map(([key, peers]) => [key === WORKSPACE_ROSTER ? null : key, peers] as const);
  return viewersAcross(placed, selfPeerId).map(({ userId, name, email, avatarUrl, places }) => ({
    userId,
    name,
    email,
    avatarUrl,
    chatIds: places,
  }));
}

/** Be in `workspaceId` (join its presence) for as long as the caller is
 *  mounted, and return who else is in it: the workspace roster joined with
 *  the rosters of `chatIds`, watched. Pass no chats where only being there
 *  matters (a chat inside the workspace). */
export function useWorkspaceViewers(
  workspaceId: string | null,
  chatIds: readonly string[],
): WorkspaceViewer[] {
  const watched = useMemo(() => chatIds.slice(0, MAX_WATCHED_CHATS), [chatIds]);
  const key = watched.join(",");
  const { client, peerId } = useRealtimePeer(workspaceId !== null || key !== "");
  const joined = usePresence(client, workspaceId ? workspaceChannel(workspaceId) : null);
  const [chatRosters, setChatRosters] = useState<ReadonlyMap<string, readonly PresencePeer[]>>(new Map());

  useEffect(() => {
    const ids = key === "" ? [] : key.split(",");
    if (client === null || ids.length === 0) {
      setChatRosters(new Map());
      return;
    }
    const releases = ids.map((chatId) =>
      observePresence(client, channelOf("chat", chatId), (peers) =>
        setChatRosters((prev) => {
          const next = new Map(prev);
          next.set(chatId, peers);
          return next;
        }),
      ),
    );
    return () => {
      for (const release of releases) release();
      setChatRosters(new Map());
    };
  }, [client, key]);

  return useMemo(() => {
    const all = new Map(chatRosters);
    all.set(WORKSPACE_ROSTER, joined);
    return workspaceViewersOf(all, peerId);
  }, [chatRosters, joined, peerId]);
}

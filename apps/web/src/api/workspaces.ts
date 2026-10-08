// Workspaces: chats that share one file tree.
//
// Every chat is in a workspace. A chat from before workspaces was adopted into
// a workspace of one that points at its own folder; a member's main workspace
// and the project workspaces they make hold several chats once the server's
// `workspaces_multi_chat` flag is on. The shapes are the generated ones, so a
// field the server adds reaches this module through `make gen-sdk` and never
// through a hand-typed copy.
//
// Which chats are in which workspace is read off the chat list (every chat row
// names its `workspace_id`), not off one request per workspace: the rail
// already holds every chat, and a request per expanded workspace would be the
// rail asking the same question twice.

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "./client";
import { ApiError } from "./errors";
import { keys } from "./keys";

export type WorkspaceRead = components["schemas"]["WorkspaceRead"];
export type WorkspaceList = components["schemas"]["WorkspaceList"];

/** How many pages the list walk follows before it stops. A page is up to a
 *  thousand workspaces, so this is a ceiling on a runaway cursor, not a limit
 *  anybody reaches. */
const PAGE_CEILING = 20;

async function listAllWorkspaces(): Promise<WorkspaceRead[]> {
  const items: WorkspaceRead[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < PAGE_CEILING; page += 1) {
    let next: WorkspaceList;
    try {
      next = await request<WorkspaceList>(
        api.GET("/api/v1/workspaces", { params: { query: cursor ? { cursor } : {} } }),
        "The workspaces could not be listed",
      );
    } catch (error) {
      // A server from before workspaces has no such route: every chat is then
      // a chat, and the rail lists them as it always did.
      if (error instanceof ApiError && error.status === 404) return [];
      throw error;
    }
    items.push(...(next.items ?? []));
    if (!next.next_cursor) break;
    cursor = next.next_cursor;
  }
  return items;
}

/** Every workspace this reader may open: their own and the ones shared with
 *  them. */
export function useWorkspaces(options: { enabled?: boolean } = {}) {
  return useQuery<WorkspaceRead[]>({
    queryKey: keys.workspaces.all,
    queryFn: listAllWorkspaces,
    enabled: options.enabled ?? true,
  });
}

/** The reader's main workspace, which the server makes the first time it is
 *  asked for. Asked only where a workspace may hold several chats: with the
 *  flag off a main workspace can hold nothing, and asking would make one (and
 *  a folder in the reader's drive) for no use. */
export function useMainWorkspace(options: { enabled: boolean }) {
  return useQuery<WorkspaceRead>({
    queryKey: keys.workspaceMain.mine,
    queryFn: () =>
      request<WorkspaceRead>(api.GET("/api/v1/workspaces/main"), "The main workspace could not be read"),
    enabled: options.enabled,
  });
}

/** One workspace. A workspace this reader may not open is the same opaque 404
 *  as one that does not exist. */
export function useWorkspace(workspaceId: string | undefined) {
  return useQuery<WorkspaceRead>({
    queryKey: keys.workspaces.one(workspaceId),
    queryFn: () =>
      request<WorkspaceRead>(
        api.GET("/api/v1/workspaces/{workspace_id}", {
          params: { path: { workspace_id: workspaceId as string } },
        }),
        "The workspace could not be read",
      ),
    enabled: Boolean(workspaceId),
  });
}

/** Make a project workspace. The server gives it a folder of its own; the
 *  client id makes a retried press land on the same workspace. */
export function useCreateWorkspace() {
  return useMutation({
    // `machinePin`: an org machine; null for the default placement over the org's
    // default machine; left out for the org's default machine when the creator may use it.
    mutationFn: (vars: { title: string; clientId?: string; machinePin?: string | null }) =>
      request<WorkspaceRead>(
        api.POST("/api/v1/workspaces", {
          body: {
            title: vars.title,
            client_id: vars.clientId ?? null,
            ...(vars.machinePin !== undefined ? { machine_pin: vars.machinePin } : {}),
          },
        }),
        "The workspace could not be made",
      ),
    // A pinned workspace is listed on its machine's page.
    meta: { invalidates: [keys.workspaces.all, keys.machines.org] },
  });
}

/** Rename, naming the version read. A workspace of one is renamed with its
 *  chat, so the chat family refreshes too. */
export function useRenameWorkspace() {
  return useMutation({
    mutationFn: (vars: { workspaceId: string; title: string; expectedVersion: number }) =>
      request<WorkspaceRead>(
        api.PATCH("/api/v1/workspaces/{workspace_id}", {
          params: { path: { workspace_id: vars.workspaceId } },
          body: { title: vars.title, expected_version: vars.expectedVersion },
        }),
        "The workspace could not be renamed",
      ),
    meta: { invalidates: [keys.workspaces.all, keys.chats.all] },
  });
}

/** End a workspace and every chat in it. The server refuses a main workspace.
 *
 *  `onDeleted` runs the moment the server has agreed, inside the mutation and
 *  so before the cache policy's refresh: a page showing the workspace, or one
 *  of its chats, leaves before anything re-reads what was just ended, and does
 *  not leave at all when the delete is refused. */
export function useDeleteWorkspace(onDeleted?: (workspaceId: string) => void) {
  return useMutation({
    mutationFn: async (workspaceId: string) => {
      await request<unknown>(
        api.DELETE("/api/v1/workspaces/{workspace_id}", {
          params: { path: { workspace_id: workspaceId } },
        }),
        "The workspace could not be deleted",
      );
      onDeleted?.(workspaceId);
    },
    meta: { invalidates: [keys.workspaces.all, keys.chats.all] },
  });
}


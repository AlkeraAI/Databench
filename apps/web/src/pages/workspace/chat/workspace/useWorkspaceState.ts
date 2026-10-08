// The wiring between a chat's stored workspace and the store the page renders.
//
// Everything with a rule in it lives in the store; this is the part that needs
// React. It reads the stored document once per chat, hands the store the saver
// that also seeds the cache, re-reads every node a file tab names so the store
// can close the ones that were already gone, and gets the pending write out on
// the way to another chat.
//
// The node reads are why a tab whose file is gone simply does not open: the
// server is asked about each one, and a 404, a 403, a folder or a node that has
// moved out of the chat's folder are all the same answer — that tab has nothing
// to show, so it is not there. It is a question about the document that was
// STORED, asked once: a file that goes while the reader has its tab open keeps
// its tab, which says so itself and offers to put the file back.

import { useEffect, useMemo } from "react";
import { useQueries } from "@tanstack/react-query";

import { useSaveWorkspace, useWorkspaceState as useStoredWorkspace } from "@/api/chats";
import { api, request } from "@/api/client";
import { ApiError } from "@/api/errors";
import type { Item } from "@/api/files";
import { keys } from "@/api/keys";

import {
  emptyEntry,
  flushWorkspace,
  setWorkspaceSaver,
  useWorkspaceStore,
  type ChatWorkspaceEntry,
  type NodeFact,
} from "./workspaceStore";

export interface WorkspaceSession {
  entry: ChatWorkspaceEntry;
  /** Whether the strip is worth drawing yet.
   *
   *  Until the stored document has been read, what the store holds is the empty
   *  workspace every chat starts from — the folder browser and nothing else —
   *  and drawing THAT is drawing a guess: a reader coming back to a chat would
   *  be shown a workspace with none of their tabs in it and have it taken back
   *  a moment later, and anything that reads the strip as it first appears —
   *  a reader's own eye included — would take that guess for the answer.
   *
   *  It is an ANSWER that is waited for, not a success: a read that was refused
   *  leaves the reader the folder browser rather than an empty pane. */
  ready: boolean;
}

/** A node the server refused outright, rather than one that has not answered
 *  yet. Only these two say the file is not the reader's to open — anything else
 *  (a flaky network, a 500) leaves the tab alone. */
function refused(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 403);
}

/**
 * Hydrate this chat's workspace, keep it honest against the drive, and save it.
 *
 * `rootPathBytes` is the chat folder's own path: a tab whose node has moved out
 * from under it is no longer this chat's file, however readable it still is.
 */
export function useWorkspaceState(
  chatId: string | undefined,
  driveId: string | undefined,
  rootPathBytes: string | null | undefined,
): WorkspaceSession {
  const stored = useStoredWorkspace(chatId);
  const save = useSaveWorkspace();
  const entry = useWorkspaceStore((state) => (chatId ? state.chats[chatId] : undefined));
  const hydrate = useWorkspaceStore((state) => state.hydrate);
  const prune = useWorkspaceStore((state) => state.prune);

  // Leaving this chat writes its pending change now rather than losing it.
  //
  // It is declared BEFORE the saver, because React runs a fiber's cleanups in
  // declaration order: the other way round the last write of a chat would go
  // out through the bare transport, seed nothing, and the older document still
  // in the cache would be read back over it on the way in.
  useEffect(() => {
    if (!chatId) return;
    return () => {
      void flushWorkspace(chatId, { keepalive: true });
    };
  }, [chatId]);

  // Route the store's writes through the mutation, so a successful save also
  // seeds the cache entry the next read of the layout would otherwise fetch.
  const mutateAsync = save.mutateAsync;
  useEffect(() => {
    setWorkspaceSaver(mutateAsync);
    return () => setWorkspaceSaver(null);
  }, [mutateAsync]);

  const document = stored.data?.state;
  useEffect(() => {
    if (chatId && document) hydrate(chatId, document);
  }, [chatId, document, hydrate]);

  const nodeIds = useMemo(() => {
    const ids = (entry?.tabs ?? [])
      .filter((tab) => tab.kind === "file" && typeof tab.node_id === "string")
      .map((tab) => tab.node_id as string);
    return [...new Set(ids)];
  }, [entry?.tabs]);

  const nodes = useQueries({
    queries: nodeIds.map((nodeId) => ({
      queryKey: keys.files.item(nodeId),
      enabled: Boolean(driveId),
      // A node the server will not hand back is an answer, not a flake: the tab
      // must go now rather than after three retries.
      retry: (failureCount: number, error: unknown) => !refused(error) && failureCount < 3,
      queryFn: () =>
        request(
          api.GET("/api/v1/files/drives/{drive_id}/items/{item_id}", {
            params: { path: { drive_id: driveId ?? "", item_id: nodeId } },
          }),
        ) as Promise<Item>,
    })),
    combine: (results) => {
      const facts: Record<string, NodeFact> = {};
      results.forEach((result, index) => {
        const nodeId = nodeIds[index];
        if (!nodeId) return;
        if (refused(result.error)) facts[nodeId] = { present: false };
        else if (result.data) {
          facts[nodeId] = result.data.trashed
            ? { present: false }
            : { present: true, kind: result.data.kind, pathBytes: result.data.pathBytes };
        }
      });
      return facts;
    },
  });

  useEffect(() => {
    if (chatId && entry?.hydrated) prune(chatId, { rootPathBytes, nodes });
  }, [chatId, entry?.hydrated, nodes, prune, rootPathBytes]);

  return { entry: entry ?? EMPTY, ready: Boolean(entry?.hydrated) || stored.isError };
}

/** What a caller renders before the chat's own document has arrived: nothing
 *  open but the folder browser, which is always there. */
const EMPTY: ChatWorkspaceEntry = emptyEntry();

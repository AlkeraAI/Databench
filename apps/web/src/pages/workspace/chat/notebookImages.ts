// Where the chat reads an output a notebook stores beside itself.
//
// `notebook.show_output` names an image, or a chart too large for its reply,
// by the notebook's path and the output's hash. The chat finds the notebook's
// node from the path the agent named and reads the output through the
// notebook's blob route, which decides the reader's access on every read as
// the notebook's own view does. A card asks only once it is on screen, and
// the answer is kept for the session: the bytes a hash names never change.

import type { StepEnvironment, StoredOutputRead } from "@alkera/ui";
import { useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { keys } from "../../../api/keys";
import { NotebookRequestError, getOutputBlob } from "../../../api/notebooks";

import { chatData } from "./data";

type ReadStored<T> = (path: string, sha256: string) => Promise<StoredOutputRead<T>>;

/** A refusal that says this reader may not read the notebook. */
function refused(error: unknown): boolean {
  return error instanceof NotebookRequestError && (error.status === 401 || error.status === 403);
}

/** The stored output's bytes, or why this reader gets none: the path names
 *  no notebook they can open, or the notebook no longer holds the output. */
function useStoredOutput(chatId: string | null): ReadStored<Blob> | undefined {
  const port = chatData().chatFiles;
  const queryClient = useQueryClient();
  return useMemo(() => {
    const locate = port?.locate;
    if (!chatId || !locate) return undefined;
    const read = async (path: string, sha256: string): Promise<StoredOutputRead<Blob>> => {
      const item = await locate(chatId, path);
      if (!item || item.kind !== "file") return { kind: "refused" };
      try {
        const blob = await getOutputBlob(item.driveId, item.nodeId, sha256);
        return blob === null ? { kind: "gone" } : { kind: "ready", value: blob };
      } catch (error) {
        if (refused(error)) return { kind: "refused" };
        throw error;
      }
    };
    return (path: string, sha256: string) =>
      queryClient.fetchQuery({
        queryKey: keys.notebooks.storedOutput(chatId, path, sha256),
        queryFn: () => read(path, sha256),
        staleTime: Infinity,
        retry: false,
      });
  }, [chatId, port, queryClient]);
}

/** The image reader the transcript's steps take, or `undefined` in a shell
 *  that cannot find a file's node from a path (the VS Code webview). */
export function useNotebookImage(chatId: string | null): StepEnvironment["notebookImage"] {
  return useStoredOutput(chatId);
}

/** The stored chart reader the transcript's steps take: the spec a notebook
 *  keeps beside itself, read through the same route as an image. */
export function useNotebookChartSpec(chatId: string | null): StepEnvironment["notebookChartSpec"] {
  const read = useStoredOutput(chatId);
  return useMemo(() => {
    if (!read) return undefined;
    return async (path: string, sha256: string): Promise<StoredOutputRead<unknown>> => {
      const found = await read(path, sha256);
      if (found.kind !== "ready") return found;
      return { kind: "ready", value: JSON.parse(await found.value.text()) as unknown };
    };
  }, [read]);
}

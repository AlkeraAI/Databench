// A link into a chat that names a file to open in the chat's workspace pane:
// `/chat/<chatId>?open=<nodeId>`. The Files page sends a notebook here, since
// the notebook editor (cells, kernel, Run) lives only in a chat's pane. The
// chat page opens the file in a tab once the pane's layout has loaded, then
// drops the parameter so a reload or Back does not open it again.

import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { useItem } from "@/api/files";
import { displayNameOf } from "@/lib/files/columns";

import { useWorkspaceStore } from "./workspace/workspaceStore";

/** The query parameter naming the file a chat link opens. */
export const OPEN_FILE_PARAM = "open";

/** The address that opens `nodeId` in the pane of chat `chatId`. */
export function chatFileHref(chatId: string, nodeId: string): string {
  return `/chat/${encodeURIComponent(chatId)}?${OPEN_FILE_PARAM}=${encodeURIComponent(nodeId)}`;
}

/** Open the file a chat link names, in that chat's pane. A node that cannot
 *  be read is dropped with the parameter: the pane has nothing to show. */
export function useOpenFileFromLink(chatId: string | undefined, driveId: string | undefined): void {
  const [searchParams, setSearchParams] = useSearchParams();
  const nodeId = searchParams.get(OPEN_FILE_PARAM) ?? undefined;
  const item = useItem(chatId === undefined ? undefined : driveId, nodeId);
  const reveal = useWorkspaceStore((state) => state.reveal);
  const settled = item.data !== undefined || item.isError;

  useEffect(() => {
    if (chatId === undefined || nodeId === undefined || !settled) return;
    const file = item.data;
    if (file !== undefined && file.kind === "file") {
      reveal(
        chatId,
        { nodeId: file.id, name: displayNameOf(file), path: file.path ?? null, parentId: file.parentId ?? null },
        { show: "file" },
      );
    }
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.delete(OPEN_FILE_PARAM);
        return next;
      },
      { replace: true },
    );
  }, [chatId, nodeId, settled, item.data, reveal, setSearchParams]);
}

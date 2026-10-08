// Which node's lease says whether this chat's files are live.
//
// The machine takes the CHAT's folder, not the working directory inside it, so
// the lease that governs every file in the pane is on the folder above the
// pane's root, and a lease frame names THAT node. Every surface in the
// workspace pane that follows the plane — the file browser's header, the
// chips on its rows, a file open in a tab — has to agree on which node that
// is, so the derivation lives here once. Both reads are the ones the pane
// already holds, under the same keys, so naming it costs no request; a chat
// old enough to have no working directory of its own IS its folder.

import { useItem } from "@/api/files";
import { isChatFilesNode } from "@/lib/files/chatFolder";

/** The node the box leases for a pane rooted at `rootNodeId`, or `undefined`
 *  until the root, and the folder above it, have been read.
 *
 *  Naming the root while the folder above is still being read would name the
 *  wrong node: a lease frame for the chat's folder landing in that window would
 *  be judged against the working directory and dropped. Unknown is the honest
 *  answer, and a caller holds what it cannot judge yet. A folder above that
 *  cannot be read settles it as the root's own. */
export function useLeaseNodeId(
  driveId: string | undefined,
  rootNodeId: string | undefined,
): string | undefined {
  const root = useItem(driveId, rootNodeId);
  const parentId = root.data?.parentId ?? undefined;
  const above = useItem(driveId, parentId);
  if (root.data === undefined) return undefined;
  if (parentId !== undefined && above.data === undefined && !above.isError) return undefined;
  return isChatFilesNode(root.data, above.data) ? above.data?.id : rootNodeId;
}

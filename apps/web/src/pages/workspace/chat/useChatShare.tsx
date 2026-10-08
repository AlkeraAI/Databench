/**
 * "Share", in the chat header, over the chat's own drive node.
 *
 * A chat is a node in the drive, so sharing a chat is sharing that node at the
 * same four rungs every other file has — there is no second permission model
 * for chats. The chat's row names the node (`files_node_id`) and the drive read
 * names the drive, which is everything {@link ShareDialog} asks for; the dialog
 * reads the roster, the ladder and the refusals itself.
 *
 * It opens IN PLACE, so the reader keeps their chat in front of them.
 *
 * The key itself belongs to the shared chrome — a glyph beside Delete, where a
 * reader looks for what they can do to the open chat — so this is a hook and
 * not a component: it hands the header the press and keeps the dialog, which
 * the surface mounts. A chat with no node — Files switched off for the org, or
 * a chat created before the object bridge existed — has nothing to share, so
 * there is no press to hand over and the header carries no key, the same way it
 * carries no Delete for a chat that is not there.
 *
 * `chatId` is `null` in any shell whose route table asks for no dialog (the
 * editor), and the Files reads below then never run: its webview has no portal
 * API behind them.
 */

import { useState, type ReactNode } from "react";

import { useChat } from "../../../api/chats";
import { useDrive } from "../../../api/files";
import { useWorkspace } from "../../../api/workspaces";
import { chatTitle } from "@/lib/chatTitle";
import { workspaceTitle } from "../workspaces/chatWorkspace";
import { ShareDialog } from "../files/ShareDialog";

export interface ChatShare {
  /** Opens the sharing dialog over this chat's node. `undefined` while there is
   *  nothing to share, which is how the header is told to render no key. */
  onShare?: () => void;
  /** The dialog, for the surface to mount beside the header. */
  dialog: ReactNode;
}

/** The header's sharing seam for `chatId`, or a dead one (`onShare`
 *  `undefined`, no dialog) for a shell or a chat with nothing to share. */
export function useChatShare(chatId: string | null): ChatShare {
  const [open, setOpen] = useState(false);
  const chat = useChat(chatId ?? undefined);
  // Held back with the chat read: a shell that shares nothing asks the portal
  // for nothing.
  const drive = useDrive({ enabled: chatId !== null });

  // A chat inside a workspace that holds several chats is shared by sharing
  // the workspace: its agent works in the workspace's whole shared tree, so a
  // share of the chat alone would hand that tree to someone the workspace was
  // never shared with. The key stays where it is and opens the workspace's
  // dialog. A workspace of one IS its chat's folder, so nothing changes there.
  const workspaceId = chatId !== null ? (chat.data?.workspace_id ?? undefined) : undefined;
  const workspace = useWorkspace(workspaceId);
  const viaWorkspace = workspace.data?.layout === "native" ? workspace.data : null;
  // Until the workspace answers there is no telling which node to share; a
  // workspace this reader cannot read (or a server without workspaces) leaves
  // the chat's own node.
  const settled = !workspaceId || workspace.isSuccess || workspace.isError;
  const nodeId = viaWorkspace ? (viaWorkspace.files_node_id ?? null) : (chat.data?.files_node_id ?? null);
  const subjectName = viaWorkspace ? workspaceTitle(viaWorkspace) : chatTitle(chat.data ?? {});
  const driveId = drive.data?.id;
  // Both ids, or there is no node to address: the dialog's own reads are keyed
  // by them and would otherwise open on a node id nobody has.
  const ready = chatId !== null && settled && Boolean(driveId) && Boolean(nodeId);

  return {
    onShare: ready ? () => setOpen(true) : undefined,
    dialog:
      open && ready && driveId && nodeId ? (
        <ShareDialog
          driveId={driveId}
          nodeId={nodeId}
          // A chat's node is stored as `<uuid>.alkerachat`, so the dialog's own
          // read would title it with a raw id. The chat knows what it is called.
          subjectName={subjectName}
          sharesConnections
          open
          onClose={() => setOpen(false)}
        />
      ) : null,
  };
}

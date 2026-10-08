/**
 * "Save as template…", over a chat in the rail or the chat that is open.
 *
 * The reusable unit is the chat: saving one copies the files it worked in and
 * carries a brief, and a new chat started from the template opens with those
 * files already in place. The question itself — the name, the brief, the
 * refusals — belongs to the dialog the Files page already asks it with, so this
 * is a seam and not a second dialog: it says WHICH chat is being saved and
 * hands the surface the props to mount that one dialog with.
 *
 * The dialog reads the chat from its node in the drive, which is also what says
 * the caller may save it at all. A surface with no drive behind it — the
 * editor's webview — asks for nothing: the read is disabled and there is no
 * press to hand over, so the surface carries no row, the same way it carries no
 * Share.
 */

import { useState } from "react";

import { useChat } from "../../../api/chats";
import { useDrive, useItem } from "../../../api/files";
import type { SaveAsTemplateDialogProps } from "../files/SaveAsTemplateDialog";

/** What the row says, wherever it is offered: the ellipsis is the promise that
 *  a question follows rather than a save happening on the press. */
export const SAVE_AS_TEMPLATE = "Save as template…";

/** Which chat a template would be made from. The rail knows this from the row
 *  it drew, so a menu on a row costs no read until the row is pressed. */
export interface TemplateSource {
  /** The chat's node in the drive — what the dialog reads the chat from. */
  nodeId: string;
}

/** What a surface mounts the one dialog with. `onSaved` is left to the caller:
 *  only it knows where a reader should land afterwards. */
export type TemplateDialogProps = Omit<SaveAsTemplateDialogProps, "onSaved">;

export interface SaveAsTemplate {
  /** Opens the dialog over one chat. */
  ask: (source: TemplateSource) => void;
  dialogProps: TemplateDialogProps;
}

/** The save-as-template seam for any chat in `driveId` — what a rail of chats
 *  needs, so every row asks the same question the same way. */
export function useSaveAsTemplateAction(driveId: string | undefined): SaveAsTemplate {
  const [asking, setAsking] = useState<TemplateSource | null>(null);
  // Read only once a row has been pressed: a rail of fifty chats reads fifty
  // nodes otherwise, for a dialog nobody has opened.
  const node = useItem(driveId, asking?.nodeId);
  return {
    ask: setAsking,
    dialogProps: {
      open: asking !== null,
      chat: node.data,
      onClose: () => setAsking(null),
    },
  };
}

export interface OpenChatTemplate {
  /** Opens the dialog over the chat that is open. `undefined` while there is
   *  nothing to save — a shell with no drive behind it, or a chat with no node
   *  — which is how a surface is told to offer no row. */
  onSaveAsTemplate?: () => void;
  dialogProps: TemplateDialogProps;
}

/** The same seam for the OPEN chat, or a dead one for a shell with no portal
 *  API behind it. */
export function useChatSaveAsTemplate(chatId: string | null): OpenChatTemplate {
  const chat = useChat(chatId ?? undefined);
  // Held back with the chat read: a shell that saves nothing asks for nothing.
  const drive = useDrive({ enabled: chatId !== null });
  const template = useSaveAsTemplateAction(drive.data?.id);

  const nodeId = chat.data?.files_node_id ?? null;
  const ready = chatId !== null && Boolean(drive.data?.id) && Boolean(nodeId);

  return {
    ...(ready && nodeId ? { onSaveAsTemplate: () => template.ask({ nodeId }) } : {}),
    dialogProps: template.dialogProps,
  };
}

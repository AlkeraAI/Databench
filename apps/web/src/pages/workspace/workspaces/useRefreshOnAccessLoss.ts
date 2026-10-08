// When the open chat's workspace stops being one this reader may open, every
// read the page draws controls from is asked again.
//
// A share revoked mid-session reaches the reader as a node frame that names
// the workspace's folder, not the chat's, so the chat row (its can_send and
// can_delete) and the drive reads behind the Files pane (Upload, New folder,
// Live) would keep the answers they were opened with. The workspace list is
// re-read on those frames, so the moment it stops listing the chat's
// workspace, the chat and the drive are re-read once and the page follows the
// server's new answer.

import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { keys } from "../../../api/keys";

/** `inWorkspace`: whether the list carries the chat's workspace; `null` while
 *  it cannot say (no chat, no workspace named, the list not yet read). */
export function useRefreshOnAccessLoss(chatId: string | undefined, inWorkspace: boolean | null): void {
  const queryClient = useQueryClient();
  const last = useRef<{ chatId: string | undefined; inWorkspace: boolean | null }>({ chatId, inWorkspace });
  useEffect(() => {
    const was = last.current;
    last.current = { chatId, inWorkspace };
    if (!chatId || was.chatId !== chatId) return;
    if (was.inWorkspace === true && inWorkspace === false) {
      void queryClient.invalidateQueries({ queryKey: keys.chats.one(chatId) });
      void queryClient.invalidateQueries({ queryKey: keys.files.all });
    }
  }, [chatId, inWorkspace, queryClient]);
}

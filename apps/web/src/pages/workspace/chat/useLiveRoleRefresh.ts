// The composer follows a role change as soon as the server makes it.
//
// What a reader may do in a chat is decided by the server once and told two
// ways: the REST rows the composer reads (the chat folder's capabilities and
// the chat row's `can_send`), and the live channel's grant, which the server
// re-sends the moment an owner raises or lowers the reader's role. The live
// file editor follows the channel, so it unlocked at once while the composer
// kept saying "You can read this chat" until a later refresh happened to
// re-read the rows: a share granted on a folder above the chat names that
// folder's node, not the chat folder the composer reads.
//
// So the channel's grant change is what re-reads the composer's rows. The
// surface holds the chat's live channel for this even when no composer is
// mounted (a reader who may not send has no field, and so no other hold on
// it); the hold is shared with the composer's own, so it costs no second
// subscription. A source with no live channel (the editor's daemon) has no
// role to follow and this does nothing.

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { keys } from "@/api/keys";

import { chatData } from "./data";

export function useLiveRoleRefresh(chatId: string | null): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!chatId) return;
    const source = chatData();
    if (!source.openLiveDraft) return;
    const { draft, release } = source.openLiveDraft(chatId);
    const stop = draft.onGrantChange((): void => {
      void queryClient.invalidateQueries({ queryKey: keys.chats.one(chatId), exact: true });
      // A grant on a folder moves what every item under it says the reader
      // may do, and the chat folder is only one of them.
      void queryClient.invalidateQueries({ queryKey: keys.files.itemAll });
    });
    return () => {
      stop();
      release();
    };
  }, [chatId, queryClient]);
}

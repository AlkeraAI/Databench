// Which chats have a live turn, and keeping the rail's status marks current.
//
// The rail draws no light of its own. A chat row and its workspace row each
// draw the status the server wrote for them, through the same StatusPill and
// the same tone table, so "Working" looks the same on a chat and on the
// workspace that holds it. What this page knows sooner than the server's lists
// (a send just pressed, a turn the open chat's publisher started or ended) is
// used only to read those lists again, never to draw a mark.

import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { keys } from "../../../api/keys";

import { sendingChatIds, useChatStore } from "./chatStore";
import { chatData } from "./data";

/** The chats with a live, unfinished turn, by id.
 *
 *  Two sources, both of them the ones the transcript itself reads:
 *
 *  - the store's in-flight sends, the instant Send is pressed;
 *  - the publisher's own `turn_state` for the chat that is OPEN, which covers
 *    a turn started somewhere else (another tab, a scheduled run).
 *
 *  The rail keeps these chats listed in a folded workspace. Each time the set
 *  changes, the chat and workspace lists are read again, so the status the
 *  server now writes for them reaches the rail without waiting for a poll.
 *  Only the open chat is asked for its turn state: following every chat would
 *  open a socket per row. */
export function useWorkingChats(
  chatId: string | undefined,
  installed: boolean,
): ReadonlySet<string> {
  const byId = useChatStore((state) => state.byId);
  const sending = useMemo(() => sendingChatIds(byId), [byId]);
  const [openWorking, setOpenWorking] = useState(false);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!installed || !chatId) {
      setOpenWorking(false);
      return;
    }
    const source = chatData();
    const read = (): void => setOpenWorking(source.turnState?.(chatId) === "working");
    read();
    return source.subscribeChat(chatId, read);
  }, [chatId, installed]);

  const working = useMemo(() => {
    if (!chatId || !openWorking) return sending;
    const ids = new Set(sending);
    ids.add(chatId);
    return ids;
  }, [sending, chatId, openWorking]);

  // Sends and the open chat's turn, spelled so a change in either is a change
  // here (a send that becomes the turn it started changes the second half).
  const turns = `${[...sending].sort().join(",")}|${openWorking && chatId ? chatId : ""}`;
  const seen = useRef(turns);
  useEffect(() => {
    if (turns === seen.current) return;
    seen.current = turns;
    void queryClient.invalidateQueries({ queryKey: keys.chats.all, exact: true });
    void queryClient.invalidateQueries({ queryKey: keys.workspaces.all, exact: true });
  }, [turns, queryClient]);

  return working;
}

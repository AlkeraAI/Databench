// What the chat lists know about chats they are not showing the inside of.
//
// The data source folds every listed chat's daemon events whether or not that
// chat's route is mounted, so the live state a row needs -- the working dot, the
// pending ask, the freshest clock -- lives in a mutable singleton. Reading that
// singleton straight from a render tears: React can commit one component's read
// and another's from either side of an event. This store is the owned copy.
// Nothing renders from the source directly; a snapshot is published on each
// event and on the minute, and every row reads that.

import { create } from "zustand";
import { chatData, type Chat, type ChatActivity } from "./data";


/** Re-publish this often so a quiet list still ages its freshness labels. */
const AGE_INTERVAL_MS = 60_000;

interface ChatActivityStore {
  /** Live per-chat state, keyed by chat id. A chat with no folded events this
   *  session is simply absent, and its row falls back to what the daemon listed. */
  activity: Record<string, ChatActivity>;
  /** Subagent chats from the last listing, each naming the parent that spawned it. */
  subagents: Chat[];
  /** What each parent's spawn card calls its child (child session id -> label). */
  labels: Record<string, string>;
  /** Follow these chats' event streams. Publishes immediately, then on every
   *  event and once a minute, until the returned unsubscribe runs. */
  watch: (chatIds: readonly string[]) => () => void;
}

export const useChatActivity = create<ChatActivityStore>((set) => {
  const publish = (): void =>
    set({
      activity: chatData().getChatActivity(),
      subagents: chatData().getSubagentChats(),
      labels: chatData().getSubagentLabels(),
    });

  return {
    activity: {},
    subagents: [],
    labels: {},
    watch(chatIds) {
      // Each watcher registers its own callback identity: the source keeps
      // subscribers in a Set, so handing every watcher the shared `publish`
      // would let the first unsubscribe detach all of them.
      const onEvent = (): void => publish();
      const offs = chatIds.map((id) => chatData().subscribeChat(id, onEvent));
      const timer = setInterval(publish, AGE_INTERVAL_MS);
      publish();
      return () => {
        for (const off of offs) off();
        clearInterval(timer);
      };
    },
  };
});

/** The store's just-sent chats fill the gap only where the fold has nothing to
 *  say. A chat with folded activity ignores them: the fold keeps consuming
 *  events while no route is mounted, so it is the authority on whether a
 *  background turn is still running, while a store entry freezes on unmount. */
export function fillSendingGap(
  activity: Record<string, ChatActivity>,
  sending: ReadonlySet<string>,
): Record<string, ChatActivity> {
  const filled = { ...activity };
  for (const chatId of sending) {
    if (!(chatId in filled)) filled[chatId] = { awaiting: true, lastInteractionAt: null };
  }
  return filled;
}

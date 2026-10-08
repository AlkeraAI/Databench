// The key a chat page mounts its surface under.
//
// A chat this page just created from its own composer is the conversation the
// reader is already looking at: the hop from `/chat` onto `/chat/<id>` must
// not remount the surface, or the bubble they sent, the composer they typed in
// and its scroll all start over — and in development React opens the fresh
// surface twice, which spent the hand-off the created chat mounted holding and
// left an empty transcript under a chat that had just been started. The key is
// latched across that one hop (the store still holds the hand-off for the id
// the route arrived at); a switch between two existing chats, or back to the
// empty composer, still remounts, as before.

import { useState } from "react";

import { useChatStore } from "./chatStore";

let composerVisits = 0;

/** Every visit to the empty composer is its own surface. */
function newKey(): string {
  composerVisits += 1;
  return `new-${composerVisits}`;
}

export function useSurfaceKey(chatId: string | undefined): string {
  const [latched, setLatched] = useState(() => ({ chatId, key: chatId ?? newKey() }));
  if (latched.chatId !== chatId) {
    const carried =
      chatId !== undefined &&
      latched.chatId === undefined &&
      useChatStore.getState().pending[chatId] !== undefined;
    setLatched({ chatId, key: carried ? latched.key : (chatId ?? newKey()) });
  }
  return latched.key;
}

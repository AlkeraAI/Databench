// Which chat the surface is on: resolved from the route, the create-flow
// handoff, and the chat list, plus the titles that identity carries.

import { useQuery } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useParams } from "react-router-dom";
import { chatKeys } from "../chatKeys";
import type { ChatHandoff } from "../openSurfaces";
import { chatData, type Chat, refetchWhileErrored } from "../data";
import { UNTITLED_CHAT } from "@/lib/chatTitle";

export interface ChatIdentity {
  chatId: string | null;
  chats: Chat[] | undefined;
  currentChat: Chat | undefined;
  title: string;
  currentTitle: string | null;
  /** The create flow resolved to a real chat id; stop forcing chatId null. */
  endCreating: () => void;
}

export function useChatIdentity({
  explicitChatId,
  handoff,
}: {
  explicitChatId?: string;
  handoff: ChatHandoff | null;
}): ChatIdentity {
  const params = useParams();
  // Both shells' routes, under the names each declares: the editor's
  // `/chat/:id` and the portal's `/chat/:chatId`. Reading only one of them left
  // the other shell's chat id permanently undefined.
  const routeChatId = explicitChatId ?? params.chatId ?? params.id;
  // A first message handed over from the sidecar means "start a NEW chat".
  // Until createChat resolves and we navigate to the real id, force chatId null
  // so the optimistic message renders on an empty chat, not over an existing one.
  const [creatingChat, setCreatingChat] = useState(() => Boolean(handoff?.pendingMessage));
  const chatsQuery = useQuery({
    queryKey: chatKeys.chats(),
    refetchInterval: refetchWhileErrored,
    queryFn: () => chatData().listChats(),
  });
  // A chat route with no chat in it is the NEW-chat surface, in both shells:
  // the composer opens empty and the crumb says so. It must never adopt the
  // newest chat from the list — that put someone else's conversation on screen
  // under "New chat", with the rail highlighting nothing and Save unwired,
  // and which chat you got depended on a race with the list query.
  const chatId = creatingChat ? null : routeChatId ?? null;
  const currentChat = chatId ? chatsQuery.data?.find((chat) => chat.id === chatId) : undefined;
  // A chat the server has not titled yet is "Untitled chat", never a blank.
  const title = chatId ? (currentChat ? currentChat.title || UNTITLED_CHAT : "Chat") : "New chat";
  const endCreating = useCallback(() => setCreatingChat(false), []);
  return {
    chatId,
    chats: chatsQuery.data,
    currentChat,
    title,
    currentTitle: currentChat?.title ?? null,
    endCreating,
  };
}

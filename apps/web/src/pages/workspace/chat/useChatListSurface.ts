// The chat family's home, whichever shell renders it.
//
// Home has no chat session, so both shells need exactly the same four daemon
// reads (the list, the model catalog, the saved defaults, the command
// vocabulary), the same account-level slash vocabulary, the same live activity
// behind each row, and the same delete. This hook is that wiring. A shell owns
// only how it words a failure and what it draws.

import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo } from "react";

import { sendingChatIds, useChatStore } from "./chatStore";

import { useChatActivity } from "./activityStore";
import { chatKeys } from "./chatKeys";
import { useSlashCommands } from "./slashCommands";
import { chatCaps, chatData, type Chat, type ChatActivity, type ChatDefaults, type ModelInfo, refetchWhileErrored, refetchWhileErroredOrEmpty, refetchWhileNoChatDefault } from "./data";
import { servesModels } from "./data/ChatDataSource";

const NOOP = (): void => {};

/** How a shell reports the outcome of a delete it asked for. `onDeleted` runs
 *  the moment the daemon confirms and before the list refetch lands, so a stale
 *  failure notice never outlives the retry that worked. */
export interface DeleteChatRequest {
  chatId: string;
  title?: string;
  onDeleted: () => void;
  onFailed: (err: unknown) => void;
}

export interface ChatListSurface {
  chats: UseQueryResult<Chat[]>;
  models: UseQueryResult<ModelInfo[]>;
  chatDefaults: ChatDefaults | undefined;
  slash: ReturnType<typeof useSlashCommands>;
  /** Live per-chat state from the folded event stream. */
  activity: Record<string, ChatActivity>;
  subagents: Chat[];
  labels: Record<string, string>;
  /** Chats this webview has just sent to, before their first event lands. */
  sending: ReadonlySet<string>;
  deleteChat: (request: DeleteChatRequest) => void;
}

export function useChatListSurface(): ChatListSurface {
  const queryClient = useQueryClient();

  const chats = useQuery({
    queryKey: chatKeys.chats(),
    refetchInterval: refetchWhileErrored,
    queryFn: () => chatData().listChats(),
  });
  // The gateway catalog for the composer's model picker. The model is pinned
  // when this composer creates the chat.
  const models = useQuery({
    queryKey: chatKeys.models(),
    enabled: servesModels(chatCaps()),
    staleTime: 5 * 60_000,
    // A transient gateway 401 makes listModels() return [] (a "success"); poll
    // while errored OR empty so the catalog self-heals without a reload.
    refetchInterval: refetchWhileErroredOrEmpty,
    queryFn: () => chatData().listModels(),
  });
  // The saved Default Chat Model + Effort, resolved against the live catalog.
  // A fresh mount re-reads it, so it tracks a change made in Preferences.
  const chatDefaults = useQuery({
    queryKey: chatKeys.chatDefaults(),
    enabled: servesModels(chatCaps()),
    staleTime: 5 * 60_000,
    refetchInterval: refetchWhileNoChatDefault,
    queryFn: () => chatData().resolveChatDefaults(),
  });
  // The daemon's command vocabulary, filtered to what runs with no chat open.
  const commands = useQuery({
    queryKey: chatKeys.commands(),
    enabled: chatCaps().opencodeActive,
    staleTime: 5 * 60_000,
    refetchInterval: refetchWhileErrored,
    queryFn: () => chatData().listCommands(),
  });
  const slash = useSlashCommands({
    atHome: true,
    chatId: "",
    currentMode: "default",
    currentTitle: null,
    daemonCommands: commands.data ?? [],
    exit: NOOP,
    onTitleChanged: NOOP,
  });

  // Every listed chat's daemon events flow through the data source whether or
  // not its route is mounted, so following the listed ids is what keeps a row's
  // dot and clock live.
  const chatIdsKey = (chats.data ?? []).map((chat) => chat.id).join("|");
  useEffect(() => {
    const ids = chatIdsKey ? chatIdsKey.split("|") : [];
    return useChatActivity.getState().watch(ids);
  }, [chatIdsKey]);

  const activity = useChatActivity((state) => state.activity);
  const subagents = useChatActivity((state) => state.subagents);
  const labels = useChatActivity((state) => state.labels);

  const storeById = useChatStore((state) => state.byId);
  const sending = useMemo(() => sendingChatIds(storeById), [storeById]);

  // The host pops a native confirm modal, so a `false` is the reader cancelling
  // and nothing needs saying. A daemon failure (the chat is locked by another
  // process) is handed back for the shell to word.
  const deleteChat = useCallback(
    ({ chatId, title, onDeleted, onFailed }: DeleteChatRequest): void => {
      void (async () => {
        try {
          const deleted = await chatData().deleteChat(chatId, title);
          if (!deleted) return;
          onDeleted();
          await queryClient.invalidateQueries({ queryKey: chatKeys.chats() });
        } catch (err) {
          onFailed(err);
        }
      })();
    },
    [queryClient],
  );

  return {
    chats,
    models,
    chatDefaults: chatDefaults.data,
    slash,
    activity,
    subagents,
    labels,
    sending,
    deleteChat,
  };
}

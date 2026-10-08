// The chat family's home under the chat-ui frontend: the chrome the chat itself
// wears, the chats, and a composer that starts a new one. This file owns the
// wiring; every organ it renders belongs to @alkera/ui.
//
// The list holds its order between bumps (a turn ending, an ask arriving), so a
// chat streaming in the background ages its label where it stands instead of
// climbing the list while it is read. See homeRows.ts for that model.

import { useMemo, useRef, useState } from "react";

import { useLocation, useNavigate } from "react-router-dom";

import { MAX_CHAT_MESSAGE_CHARS } from "@alkera/sdk";
import { ChatHome, Composer, currentBrand, Notice, type ChromeLinkId } from "@alkera/ui";
import "@alkera/ui/chat/styles";
import "@alkera/ui/chat/fonts";

import { DRAFT_CHAT_KEY, useChatStore } from "./chatStore";
import { useRememberedDraftPicks } from "./draftPicks";

import { composerMetaToSendOptions } from "./adapters";
import { chatRoutes } from "./chatRoutes";
import { stackedTarget } from "./controller";
import { openChatInEditor, startNewChat } from "./openSurfaces";
import { carriedDraft } from "./startChatWith";
import { ChatChrome } from "./ChatChrome";
import {
  NO_ORDER,
  applyOrder,
  buildRows,
  nextOrder,
  orderEntries,
  type OrderEntry,
  type OrderMemory,
} from "./homeRows";
import { MODE_OPTIONS, effortOptions, slashOptions } from "./options";
import { servesModels, switchableModes } from "./data/ChatDataSource";
import { resolveModelChoice } from "./modelChoice";
import { useChatListSurface } from "./useChatListSurface";
import { useChromeLinks } from "./useChromeLinks";
import { useHostDark } from "./useHostDark";
import "./chat-surface.css";
import { chatCaps, chatData, chatHost, errorText, type Chat, type SendOptions } from "./data";
import { useChatFilesWiring } from "./chatFilesWiring";

/** The chrome names the place the way the list does. */
const HOME_TITLE = "Chats";
const PLACEHOLDER = "Plan, build, or review something new…";
const LOADING = "Loading chats…";
const NO_CHATS = "No chats yet. Start one below.";

/** What the surface has to tell the reader about an action they just took. */
interface HomeNotice {
  level: "danger" | "warning";
  title: string;
  body: string;
}

/** The account-level commands run right here; everything else needs a chat's
 *  daemon session, and home has none. */
const SLASH_NOTICE: HomeNotice = {
  level: "warning",
  title: "That command runs inside a chat",
  body: "Open a chat, or send a message to start one.",
};

/** The order the reader is looking at, carried across renders: it changes only
 *  when a chat arrives, leaves, or bumps. Leaving home ends the visit, so the
 *  next arrival is seeded freshest-first again. */
function useHeldOrder(entries: readonly OrderEntry[]): readonly string[] {
  const held = useRef<OrderMemory>(NO_ORDER);
  return useMemo(() => {
    held.current = nextOrder(held.current, entries);
    return held.current.order;
  }, [entries]);
}

export function ChatHomeSurface() {
  const navigate = useNavigate();
  const dark = useHostDark();
  // The words a "Start a new chat with <model>" carried over from another chat.
  const carried = carriedDraft(useLocation().state);

  const list = useChatListSurface();
  const { activity, chats: chatsQuery, models: modelsQuery, sending, slash, subagents } = list;
  // This composer has no panel affordance and home has no transcript to carry
  // a daemon outcome, so only commands that act through the host run here
  // (`/usage` presents as a panel and stays chat-only in this frontend).
  const homeCommands = useMemo(
    () => slash.commands.filter((command) => command.present !== "panel"),
    [slash.commands],
  );

  const view = useMemo(() => {
    const chats = chatsQuery.data ?? [];
    return {
      rows: buildRows({ chats, subagents, activity, sending, labels: list.labels }),
      entries: orderEntries(chats, activity),
      titles: new Map(chats.map((chat) => [chat.id, chat.title] as const)),
      parentOf: new Map(
        subagents.flatMap((chat) =>
          chat.parentSessionId ? [[chat.id, chat.parentSessionId] as const] : [],
        ),
      ),
    };
  }, [chatsQuery.data, activity, subagents, list.labels, sending]);
  const order = useHeldOrder(view.entries);
  const rows = useMemo(() => applyOrder(view.rows, order), [view.rows, order]);

  const [notice, setNotice] = useState<HomeNotice | null>(null);

  const openRow = (chatId: string) => {
    // A subagent chat is read-only, and the controller reads that from the back
    // target: a stacked chat whose parent is a DIFFERENT chat is a drill-in.
    const parent = view.parentOf.get(chatId);
    if (parent) {
      navigate(
        stackedTarget(chatRoutes().subagent(chatId), `/chat/${encodeURIComponent(parent)}`),
      );
      return;
    }
    openChatInEditor(chatId, view.titles.get(chatId) ?? chatId, { navigate });
  };

  const deleteChat = (chatId: string) => {
    list.deleteChat({
      chatId,
      title: view.titles.get(chatId),
      onDeleted: () => setNotice(null),
      onFailed: (err) =>
        setNotice({ level: "danger", title: "Couldn't delete the chat", body: errorText(err) }),
    });
  };

  // Home has no transcript, so the artifacts link renders without a count
  // rather than claiming zero.
  const links = useChromeLinks();
  const openLink = (id: ChromeLinkId): void => {
    if (id === "lineage") void chatHost().runCommand({ command: "alkera.openLineage" });
    // A chat's results belong to a chat, and there is none here, so the link
    // stays a door to the knowledge side of the workspace.
    else void chatHost().runCommand({ command: "alkera.openContext" });
  };

  const models = modelsQuery.data ?? [];
  // The picks survive a reload until the chat they were made for exists.
  useRememberedDraftPicks();
  const prefs = useChatStore((state) => state.composerPrefs[DRAFT_CHAT_KEY]);
  const setComposerPref = useChatStore((state) => state.setComposerPref);
  const chatDefaults = list.chatDefaults;
  const modelOptions = models.map((entry) => ({ value: entry.id, label: entry.displayName }));
  const choice = resolveModelChoice(models, chatDefaults, prefs);
  const { model, efforts } = choice;
  const effort = choice.effort ?? "";
  // The stance the chat this composer starts is pinned to: one the reader
  // picked here, or the one the server resolved for their next chat and handed
  // back with the new-chat seed. Nothing else may ride the create — a source's
  // own fallback is this surface guessing at that answer, and a create that
  // names the guess writes it onto the chat row in place of the saved default
  // the server would otherwise have applied.
  const stance = prefs?.mode ?? chatDefaults?.permissionMode ?? undefined;
  // Until the seed lands there is no stance to promise, so the chip leaves the
  // rail rather than naming one the create is not asking for.
  const seedPending = servesModels(chatCaps()) && chatDefaults === undefined;
  const mode = stance ?? chatCaps().fixedPermissionMode ?? "";
  // The stances this source will open a NEW chat in — the same narrowing the
  // chat page makes, read from the same place, so home and chat cannot offer
  // different menus for the same session.
  const allowedModes = switchableModes(chatCaps());
  const homeModeOptions =
    stance === undefined && seedPending
      ? []
      : MODE_OPTIONS.filter((option) => allowedModes.has(option.value));

  // What the composer's pasted images and attached files go into. There is no
  // chat here yet, so the uploader holds them and this opens one on Send —
  // under the same picks the first message carries. The opened chat is handed
  // to the chat surface, which sends the message INTO it rather than opening a
  // second one. A source that cannot open a chat ahead of its message offers
  // no attach door at all.
  const openedRef = useRef<Chat | null>(null);
  const sendOptions = (): SendOptions | undefined =>
    servesModels(chatCaps())
      ? composerMetaToSendOptions({ model, effort: effort || undefined, mode: stance }, models)
      : undefined;
  const optionsRef = useRef(sendOptions);
  optionsRef.current = sendOptions;
  const canOpen = typeof chatData().startChat === "function";
  const openChatForFiles = useMemo(
    () =>
      canOpen
        ? async (title: string): Promise<Chat> => {
            if (openedRef.current) return openedRef.current;
            const source = chatData();
            if (!source.startChat) throw new Error("this workspace cannot open a chat before its first message");
            const chat = await source.startChat(title, optionsRef.current());
            openedRef.current = chat;
            return chat;
          }
        : undefined,
    [canOpen],
  );
  const chatFiles = useChatFilesWiring(null, openChatForFiles);

  const send = (text: string) => {
    if (text.startsWith("/")) {
      // The home menu's commands run through the host; any other slash line
      // needs a session this surface does not have.
      const name = text.slice(1).split(/\s/, 1)[0];
      const command = homeCommands.find((candidate) => candidate.trigger === name);
      if (command) {
        setNotice(null);
        slash.onCommandRun(command);
        return;
      }
      setNotice(SLASH_NOTICE);
      return;
    }
    setNotice(null);
    // The model and effort ride the CREATE on any source that serves a
    // catalogue — a cloud chat pins them on the chat row, an editor chat on the
    // manifest — so the first turn already runs on what the reader picked.
    const options = sendOptions();
    // A chat already opened for this message's files takes it as its first
    // message; the ref is cleared so a later send from this composer opens its
    // own chat rather than writing into one that already has a message.
    const into = openedRef.current;
    openedRef.current = null;
    startNewChat(text, options, { navigate }, into ?? undefined);
  };

  const focusComposer = () => {
    document.querySelector<HTMLElement>(`[aria-label="Message ${currentBrand().productName}"]`)?.focus();
  };

  const noModels = servesModels(chatCaps()) && modelsQuery.isSuccess && modelOptions.length === 0;
  const header = (
    <>
      <ChatChrome
        title={HOME_TITLE}
        links={links}
        // This surface IS where New chat leads, so the key puts the cursor in
        // the composer already on it — but only in a shell that carries the
        // key at all (`chatRoutes().newChat`).
        onNewChat={chatRoutes().newChat ? focusComposer : undefined}
        onOpenLink={openLink}
      />
      {chatsQuery.isError ? (
        <div className="chat-shell-problem">
          <Notice
            level="danger"
            title="Chats unavailable"
            body={`Couldn't load the workspace. ${errorText(chatsQuery.error)}`}
          />
        </div>
      ) : null}
      {modelsQuery.isError || noModels ? (
        <div className="chat-shell-problem">
          <Notice
            level="danger"
            title="Models unavailable"
            body={
              modelsQuery.isError
                ? `Couldn't load models. ${errorText(modelsQuery.error)}`
                : "No models available from the model gateway."
            }
          />
        </div>
      ) : null}
      {notice ? (
        <div className="chat-shell-problem">
          <Notice
            level={notice.level}
            title={notice.title}
            body={notice.body}
            onDismiss={() => setNotice(null)}
          />
        </div>
      ) : null}
    </>
  );

  // The composer waits for the list: a chat cannot start before the daemon has
  // answered, and an empty list under a live composer would read as the truth.
  const settled = !chatsQuery.isPending && !chatsQuery.isError;

  return (
    <div className="chat-root chat-shell-fill" data-theme={dark ? "dark" : undefined}>
      <ChatHome
        chats={rows}
        header={header}
        empty={chatsQuery.isError ? null : chatsQuery.isPending ? LOADING : NO_CHATS}
        onOpen={openRow}
        onOpenInEditor={openRow}
        onDelete={deleteChat}
        dock={
          settled ? (
            <Composer
              placeholder={PLACEHOLDER}
              maxMessageChars={MAX_CHAT_MESSAGE_CHARS}
              modes={homeModeOptions}
              mode={mode}
              onModeChange={
                homeModeOptions.length > 0
                  ? (next) => setComposerPref(DRAFT_CHAT_KEY, { mode: next })
                  : undefined
              }
              models={modelOptions}
              model={model}
              onModelChange={(next) =>
                setComposerPref(DRAFT_CHAT_KEY, { model: next, effort: undefined })
              }
              efforts={effortOptions(efforts)}
              effort={effort}
              onEffortChange={(next) => setComposerPref(DRAFT_CHAT_KEY, { model, effort: next })}
              slashCommands={slashOptions(homeCommands)}
              uploader={chatFiles.uploader}
              onSend={send}
              draft={carried ?? undefined}
            />
          ) : undefined
        }
      />
    </div>
  );
}

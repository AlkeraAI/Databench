// Binding the data source's chat-files port to one chat: the resolver the
// transcript renders images through, and the uploader the composer stages
// pastes with. Both are `null`/`undefined` for a source with no port, so the
// composer offers no paste and every image is a placeholder — never a call
// into nothing.
//
// A chat that does not exist yet (the composer before its first send) has no
// folder to stage into, so its uploader holds what the reader attaches and
// uploads on send: `openChat` opens the chat for the message first, every
// held file lands in it, and the message goes out naming them. Without an
// opener the empty composer offers no door at all.
//
// Where a clicked file OPENS is the shell's call, not the transcript's. A
// shell with somewhere of its own to put it — a tab beside the chat — passes
// `openItem` and gets the node itself; one with nowhere keeps the port's own
// door, which on the web is a new browser tab. A port that cannot look a path
// up (the VS Code webview only hands the path to its host) keeps its door too,
// so an opener it could never feed does not swallow the click.
//
// A port that CAN look a path up also lets the transcript say which file each
// reference is before it is clicked, and clicking it puts that file in front of
// the reader in the workspace's own file browser rather than in a browser
// window: the folder it sits in is opened, its row is selected and scrolled to,
// and the file itself opens in a tab — unless the reader already has it open,
// in which case the click is a jump to that tab. That is the default here
// because the workspace pane is where the portal's chat keeps its files; a
// shell with a different place for them passes its own `revealItem`.

import { useCallback, useMemo, useRef } from "react";

import type { ChatFileRef, ChatFilesResolver, ComposerUploader } from "@alkera/ui";

import { chatData, type Chat } from "./data";
import type { ChatFileLocation } from "./data/chatFiles";
import { tabForNode, useWorkspaceStore } from "./workspace/workspaceStore";

export interface ChatFileDoors {
  /** Open a located file where this shell puts it. */
  openItem?: (item: ChatFileLocation) => void;
  /** Put a located file in front of the reader in this shell's file browser.
   *  Defaults to the workspace pane beside the chat. */
  revealItem?: (ref: ChatFileRef) => void;
}

/** Show a located file in the workspace pane.
 *
 *  A file the reader already has OPEN is the exception: the tab they opened is
 *  a better answer to "take me to it" than the folder it came from, so the
 *  click jumps to that tab and leaves the browser and the rest of the strip
 *  exactly as they were. Activating it is also what brings the tab back into
 *  view when the strip has overflowed.
 *
 *  Otherwise the Files tab walks to the folder the file sits in and selects it,
 *  the file opens in a tab of its own, and the Files tab is the one in front —
 *  the reader asked "which file is this", and the answer is the row,
 *  highlighted, in its place. */
function revealInWorkspace(chatId: string, ref: ChatFileRef): void {
  const store = useWorkspaceStore.getState();
  const open = tabForNode(store.chats[chatId]?.tabs ?? [], ref.nodeId);
  if (open) {
    store.activate(chatId, open.id);
    return;
  }
  store.reveal(
    chatId,
    { nodeId: ref.nodeId, name: ref.name, path: ref.path, parentId: ref.parentId },
    { show: "browser" },
  );
}

export function useChatFilesWiring(
  chatId: string | null,
  openChat?: (title: string) => Promise<Chat>,
  doors?: ChatFileDoors,
): {
  resolver: ChatFilesResolver | null;
  uploader: ComposerUploader | undefined;
} {
  const port = chatData().chatFiles;
  // The chat the held uploads go into, once the send has opened it.
  const opened = useRef<Chat | null>(null);
  const opener = useRef(openChat);
  opener.current = openChat;
  const canOpen = Boolean(openChat);
  // Read at click time, so a shell that re-declares its opener every render
  // does not re-make the resolver and re-resolve every image in the transcript.
  const toTab = useRef(doors?.openItem);
  toTab.current = doors?.openItem;
  const toBrowser = useRef(doors?.revealItem);
  toBrowser.current = doors?.revealItem;
  const canOpenItem = Boolean(doors?.openItem) && Boolean(port?.locate);
  const beforeSend = useCallback(async (message: string): Promise<void> => {
    const open = opener.current;
    if (!open) throw new Error("this chat cannot be opened for its files");
    opened.current = await open(message);
  }, []);
  return useMemo(() => {
    if (!port) return { resolver: null, uploader: undefined };
    if (chatId) {
      // A folder is not something a transcript reference opens, so a path that
      // walks to one answers the same "nowhere" a missing path does — the same
      // rule `resolveUrl` already applies to the bytes.
      const locate = port.locate
        ? async (path: string): Promise<ChatFileRef | null> => {
            const item = await port.locate?.(chatId, path);
            if (!item || item.kind !== "file") return null;
            return { nodeId: item.nodeId, parentId: item.parentId, name: item.name, path: item.path };
          }
        : undefined;
      const reveal = port.locate
        ? (ref: ChatFileRef) => {
            const own = toBrowser.current;
            if (own) own(ref);
            else revealInWorkspace(chatId, ref);
          }
        : undefined;
      // The port's own door is a new browser window. A shell that can look a
      // path up has the file's own node and somewhere in the page to put it, so
      // the window is the LAST resort: an opener if the shell gave one, the file
      // browser beside the chat otherwise, and only a shell that can resolve
      // nothing leaves the page.
      const openPath = canOpenItem
        ? (path: string) => {
            void port.locate?.(chatId, path).then((item) => {
              if (item) toTab.current?.(item);
            });
          }
        : locate && reveal
          ? (path: string) => {
              void locate(path).then((ref) => {
                if (ref) reveal(ref);
              });
            }
          : port.openPath
            ? (path: string) => port.openPath?.(chatId, path)
            : undefined;
      return {
        resolver: { resolveUrl: (path) => port.resolveUrl(chatId, path), locate, reveal, openPath },
        uploader: { maxBytes: port.maxBytes, upload: (file, hint) => port.upload(chatId, file, hint) },
      };
    }
    if (!canOpen) return { resolver: null, uploader: undefined };
    return {
      resolver: null,
      uploader: {
        maxBytes: port.maxBytes,
        timing: "on-send",
        beforeSend,
        upload: (file, hint) => {
          const chat = opened.current;
          if (!chat) return Promise.reject(new Error("the chat was not opened before its files"));
          return port.upload(chat.id, file, hint);
        },
      },
    };
  }, [port, chatId, canOpen, canOpenItem, beforeSend]);
}

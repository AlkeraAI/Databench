// Deleting a chat: one confirm step, the DELETE, and — where the chat being
// deleted is the one on screen — the reader is taken away from it. The rail
// re-reads itself through the mutation's invalidation, so the deleted chat
// leaves the list without anyone wiring a refetch.
//
// The question is asked in the product's own dialog, not the browser's. A
// `window.confirm` is a modal of the BROWSER: it carries the origin in its
// title, cannot be styled, cannot name its destructive action ("OK" is what it
// offers for deleting a colleague's work), takes the whole window rather than
// the surface it belongs to, and is suppressible per-tab — a reader who has
// ticked "don't show me these again" deletes chats on a single click with no
// question at all. One dialog, asked the same way from the header and from a
// rail row, is also the only way the two stay the same question.
//
// It is the confirmation every other setting shows, in its destructive tone:
// the dialog opens with Cancel under the finger and refuses the Enter a reader
// on their way somewhere else would press.
//
// The hop away happens the moment the DELETE answers yes (inside the
// mutation, before its invalidation), NOT in the mutation's callsite
// `onSuccess`. A refused delete leaves the reader on the chat with the
// server's sentence. Every callback a callsite hands `mutate` runs after the cache's
// own invalidation has been awaited, and what that invalidation refetches
// includes the queries of the chat the DELETE just took away — which answer
// 404. So the callback lands seconds after the server has already agreed, and
// for those seconds the reader sits in front of the transcript, the files dock
// and the live composer of a chat that is gone: exactly the thing they asked to
// be rid of. Leaving first is also what makes the refetch cheap — the surface
// that was asking for the dead chat is no longer mounted to ask again.
//
// The bookkeeping leaves with the reader, for the same reason: what it cancels
// is a debounced PUT of the chat's open tabs, and a callback that runs seconds
// later is a callback that runs after that write has already gone out — to a
// chat the server no longer has. Both things it forgets are this browser's
// notes, held on the server as well, so a delete the server refuses costs the
// reader the two of them and nothing else: the chat is still listed in the
// rail, and opening it reads its tabs back and remembers it again.

import { useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import { ConfirmDialog } from "@alkera/ui";

import { useChat, useDeleteChat } from "../../../api/chats";
import { refusalSentence } from "../../../api/errors";

import { chatRoutes } from "./chatRoutes";
import { forgetDeletedChat } from "./lastOpenChat";
import { forgetChatPane } from "./workspace/workspaceStore";
import { granted } from "@/lib/capabilities";

/** What the dialog asks, and what the key that answers it says. The heading
 *  carries the question; the line under it carries the one fact a reader needs
 *  to answer it, which is what a delete takes with it.
 *
 *  The heading NAMES the chat. A rail row's menu opens this question over a
 *  chat that is not the one on screen, so a constant heading asks about "this
 *  chat" while the reader is looking at another — and the only way to tell
 *  which one is about to go was to cancel and count the rows. */
export const DELETE_CHAT_TITLE = "Delete this chat?";
/** A deleted chat's folder — its transcript and every file it holds — goes to
 *  the drive's Trash, which is where the reader looks for it afterwards. */
export const DELETE_CHAT_BODY = "The chat and its files move to Trash.";
export const DELETE_CHAT_KEY = "Delete";

/** The longest title the heading spells out whole. A title has no length cap,
 *  and a heading that wraps over five lines pushes the answer keys off a phone. */
const DELETE_TITLE_CHARS = 80;

/** The question over one chat. A chat whose title the server has not derived
 *  yet has nothing to name, so it keeps the unnamed form rather than asking
 *  about an empty string. A title that is itself a question already ends in
 *  the question mark the heading needs, and a long one is cut with an
 *  ellipsis. */
export function deleteChatTitle(title: string | null | undefined): string {
  const named = title?.trim();
  if (!named) return DELETE_CHAT_TITLE;
  const shown =
    named.length > DELETE_TITLE_CHARS ? `${named.slice(0, DELETE_TITLE_CHARS - 1).trimEnd()}…` : named;
  return shown.endsWith("?") ? `Delete ${shown}` : `Delete ${shown}?`;
}

/** One delete question for however many chats a surface can delete.
 *
 *  `perform` is what happens once the reader has said yes — it differs by where
 *  the press came from (the header leaves the chat, a rail row leaves only if
 *  the row IS the open chat), and neither of those belongs in the dialog. */
export interface ChatDeleteConfirm {
  /** Opens the question over one chat, under that chat's own name. */
  ask: (chatId: string, title?: string | null) => void;
  /** The dialog itself. Mounted ONCE by the surface, outside any row: a row
   *  that is re-rendered, or scrolled past, must not take the question with
   *  it. */
  dialog: ReactNode;
}

export function useChatDeleteConfirm(perform: (chatId: string) => void): ChatDeleteConfirm {
  const [asking, setAsking] = useState<{ chatId: string; title: string | null } | null>(null);
  const close = (): void => setAsking(null);
  return {
    ask: (chatId, title) => setAsking({ chatId, title: title ?? null }),
    dialog: (
      <ConfirmDialog
        open={asking !== null}
        onClose={close}
        title={deleteChatTitle(asking?.title)}
        consequence={DELETE_CHAT_BODY}
        confirmLabel={DELETE_CHAT_KEY}
        tone="destructive"
        onConfirm={() => {
          const chatId = asking?.chatId;
          close();
          if (chatId) perform(chatId);
        }}
      />
    ),
  };
}

/** The sentence a refused delete shows: the server's own, which names who may
 *  delete, and a plain one when the refusal carried none. */
export const DELETE_CHAT_FAILED = "This chat could not be deleted.";

export function deleteRefusal(error: unknown): string {
  return refusalSentence(error, { fallback: DELETE_CHAT_FAILED });
}

/** What deleting `chatId` costs this browser, and where it leaves the reader.
 *  `onDeleted` runs once the server has agreed; `onRefused` gets the sentence
 *  to show when it did not, and the reader stays where they are. */
export function useDeleteOneChat(
  onDeleted?: (chatId: string) => void,
  onRefused?: (sentence: string) => void,
): (chatId: string) => void {
  const deleteChat = useDeleteChat(onDeleted);
  return (chatId: string): void => {
    // A deleted chat is nobody's last-open chat any more: leaving the id behind
    // would have the portal's Chat leaf lead back to a transcript that is gone.
    forgetDeletedChat(chatId);
    // Its open tabs go with it. The document lives on the chat row, so there is
    // nothing left to write the pending save to — a debounced write that
    // survived the delete would be a PUT to a chat the server no longer has,
    // and a new chat that reused the id would inherit tabs pointing at a
    // stranger's nodes.
    forgetChatPane(chatId);
    deleteChat.mutate(chatId, { onError: (error) => onRefused?.(deleteRefusal(error)) });
  };
}

/** The header's delete action for the open chat. `onDelete` is `undefined`
 *  while there is no open chat to delete (the home surface carries the same
 *  header), and the dialog is mounted by the surface either way. */
export interface DeleteOpenChat {
  onDelete: (() => void) | undefined;
  dialog: ReactNode;
}

export function useDeleteChatAction(chatId: string | null | undefined): DeleteOpenChat {
  const navigate = useNavigate();
  const [refusal, setRefusal] = useState<string | null>(null);
  const remove = useDeleteOneChat(() => navigate(chatRoutes().home, { replace: true }), setRefusal);
  // The open chat's own row, read off the cache the page already holds — the
  // header knows the id it can delete, and the heading has to say the name.
  const chat = useChat(chatId ?? undefined);
  const confirm = useChatDeleteConfirm((id) => {
    setRefusal(null);
    remove(id);
  });
  // Deleting is the owner's or an org admin's, and only a server that said yes
  // gets the key offered: a row that leaves it unsaid is not a grant.
  const mayDelete = granted(chat.data?.can_delete);
  return {
    onDelete: chatId && mayDelete ? () => confirm.ask(chatId, chat.data?.title) : undefined,
    dialog: (
      <>
        {confirm.dialog}
        {refusal ? (
          <p className="chat-delete-refusal" role="alert">
            {refusal}
          </p>
        ) : null}
      </>
    ),
  };
}

// Copying a chat: the owner's duplicate, or a copy into someone else's drive.
//
// One key, two readings. The chat's owner gets "Duplicate": a second chat
// holding the same transcript, opening on "<title> (copy)". Anyone else gets
// "Copy to my drive": the chat lands in THEIR Chats folder, opening on the
// source's title. Either way the name is asked for BEFORE the copy is made —
// the copy is opened the moment it exists, and a name decided afterwards would
// mean renaming a chat already on screen.
//
// And either way the page lands IN the copy. The copy is the chat the reader
// meant to work in — the original is still in the rail, one press away — and
// the chat page remembers whatever it opens, so landing there is also what
// makes the copy the chat the nav's Chat leaf leads back to.
//
// The request is the Files duplicate over the chat's own node, which is how the
// server knows the node is a chat: a chat with no node (Files off, or a chat
// from before the object bridge) has nothing to copy, and the surface carries
// no key for it — the same way it carries no Share.

import { useRef, useState, type ReactElement } from "react";
import { useNavigate } from "react-router-dom";

import { useCurrentUser } from "../../../api/auth";
import { useChat } from "../../../api/chats";
import { useDrive, useDuplicateItem } from "../../../api/files";

import { chatTitle } from "@/lib/chatTitle";

import { DuplicateChatDialog } from "./DuplicateChatDialog";
import { refusalSentence } from "../../../api/errors";

export const DUPLICATE_CHAT = "Duplicate";
export const COPY_CHAT_TO_MY_DRIVE = "Copy to my drive";
/** What the server appends to the owner's copy, spelled here so the field opens
 *  on the name the copy will actually have. */
export const COPY_SUFFIX = " (copy)";

/** Which chat a copy is of. The rail knows all of this from the row it drew, so
 *  a menu on a row costs no read of its own. */
export interface CopySource {
  chatId: string;
  /** The chat's node in the drive — the thing the duplicate route copies. */
  nodeId: string;
  title: string;
  /** Whether the reader owns it, which decides the wording and the name. */
  owner: boolean;
}

/** What the copy key says for a chat this reader owns, or does not. */
export function copyLabel(owner: boolean): string {
  return owner ? DUPLICATE_CHAT : COPY_CHAT_TO_MY_DRIVE;
}

export interface ChatDuplicate {
  /** Opens the naming dialog for one chat. */
  ask: (source: CopySource) => void;
  /** True while a copy is in flight, so a key is not pressed twice. */
  pending: boolean;
  /** The dialog, while one is open. The surface renders it; this hook owns it,
   *  so every entry point to a copy — a header key, a rail row's menu — asks
   *  the same question the same way. */
  dialog: ReactElement | null;
}

/** The naming step and the copy itself, for any chat in `driveId`. */
export function useChatDuplicate(driveId: string | undefined): ChatDuplicate {
  const navigate = useNavigate();
  const duplicate = useDuplicateItem();
  const [asking, setAsking] = useState<CopySource | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  // Pressed twice before the first request answers: the mutation's own
  // `isPending` flips on the next render, which two presses in one tick do not
  // wait for, so the guard is a ref set at the press and cleared at the answer.
  const inFlight = useRef(false);

  const run = (name: string): void => {
    if (!driveId || !asking || inFlight.current) return;
    inFlight.current = true;
    setRefusal(null);
    duplicate.mutate(
      { driveId, itemId: asking.nodeId, name },
      {
        onSettled: () => {
          inFlight.current = false;
        },
        onError: (error) => setRefusal(refusalSentence(error, { fallback: "This chat could not be copied." })),
        onSuccess: (made) => {
          setAsking(null);
          // Both readings land in the copy: it is the chat the press was for,
          // and the page remembers what it opens.
          if (made.chatId) navigate(`/chat/${encodeURIComponent(made.chatId)}`);
        },
      },
    );
  };

  return {
    ask: (source) => {
      setRefusal(null);
      setAsking(source);
    },
    pending: duplicate.isPending,
    dialog: asking ? (
      <DuplicateChatDialog
        owner={asking.owner}
        suggestedName={asking.owner ? `${asking.title}${COPY_SUFFIX}` : asking.title}
        error={refusal}
        pending={duplicate.isPending}
        onCancel={() => setAsking(null)}
        onSubmit={run}
      />
    ) : null,
  };
}

export interface ChatCopy {
  /** What the key says: whose copy this would be. */
  label: string;
  /** Opens the naming dialog. `undefined` while there is nothing to copy, which
   *  is how a surface is told to render no key. */
  onCopy?: () => void;
  pending: boolean;
  dialog: ReactElement | null;
}

/** The copy seam for the OPEN chat, or a dead one (`onCopy` `undefined`) for a
 *  shell with no portal API behind it or a chat with no node. */
export function useChatCopyAction(chatId: string | null): ChatCopy {
  const chat = useChat(chatId ?? undefined);
  const me = useCurrentUser();
  // Held back with the chat read: a shell that copies nothing asks for nothing.
  const drive = useDrive({ enabled: chatId !== null });
  const duplicate = useChatDuplicate(drive.data?.id);

  const nodeId = chat.data?.files_node_id ?? null;
  const owner = Boolean(me.data?.id) && chat.data?.owner_user_id === me.data?.id;
  const ready = chatId !== null && Boolean(drive.data?.id) && Boolean(nodeId);

  return {
    label: copyLabel(owner),
    pending: duplicate.pending,
    dialog: duplicate.dialog,
    onCopy:
      ready && chatId && nodeId
        ? () =>
            duplicate.ask({
              chatId,
              nodeId,
              title: chatTitle(chat.data ?? {}),
              owner,
            })
        : undefined,
  };
}

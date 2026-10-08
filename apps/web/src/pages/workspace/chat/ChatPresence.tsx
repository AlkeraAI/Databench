// Who ELSE has this chat open, as a stack of faces at the top right.
//
// A chat document is single-WRITER and multi-READER by design: the machine's
// publisher is the only peer that appends to the transcript, but anyone shared
// the chat may read it, and anyone shared it at `writer` may steer the shared
// composer draft. This is the part of that a reader can see. The way a shared
// document does it: one face per PERSON (two of their tabs are one face) in a
// stack, and a "+N" past five.
//
// THIS TAB is never among them. You already know you have the chat open in the
// window you are looking at — a face for it says nothing, and when you are the
// only one here it is the ONLY thing on the row, which reads as a stray chip
// rather than as company. Every other socket is company, your own second window
// included: that face is the other place this chat is open, and without it a
// caret moving in a chat you appear to be alone in has nobody behind it. An
// empty stack draws nothing at all: no pill, no "+0", no gap where a chip
// would be.
//
// A face is the person's picture when their profile carries one, else their
// initials on a colour that is theirs for good — the same colour their caret
// wears in the composer, so a name on a caret and a face in the stack read as
// one person without a legend. The tooltip names them and, where the rung is
// known, what they may do here. It is the shared Tooltip rather than a native
// `title`: a title tip waits a second or more, never opens from the keyboard,
// and some hosts never draw it at all, so a face that carried only a title read
// as two letters to everyone who hovered it. Each face takes focus so Tab
// reaches the same name a pointer does.

import type { ReactElement } from "react";

import { Avatar, Tooltip } from "@alkera/ui";

import { useCurrentUser } from "../../../api/auth";
import {
  faceTitle,
  initialsOf,
  rosterViewers,
  usePresence,
  useRealtimePeer,
  type PresencePeer,
  type PresenceViewer,
} from "../../../api/realtime/presence";
import { channelOf } from "../../../api/realtime/wsClient";
import { hueOf } from "../../../lib/personHue";

/** How many faces are drawn before the rest become "+N". */
export const MAX_FACES = 5;

/** Which peer on the roster is THIS TAB: the id the server minted for this
 *  socket. A tab is the unit of presence, not a person — the same person in a
 *  second window is company the same way a colleague is, and is shown. */
export interface PresenceSelf {
  peerId: string | null;
}

/** The people behind a chat's roster, in roster order, WITHOUT this tab. Your
 *  own face in another window is not noise: it is the second place your draft
 *  is being read from, and the one thing that explains a caret moving in a
 *  chat you are alone in. */
export function viewersOf(peers: readonly PresencePeer[], self: PresenceSelf): PresenceViewer[] {
  return rosterViewers(peers, self.peerId);
}

/** What the stack says out loud: everyone it could name and a count of the
 *  ones it could not, so the label is never a lie in either direction. */
export function presenceLabel(viewers: readonly PresenceViewer[]): string {
  const named = viewers.filter((v) => v.name !== "").map((v) => v.name);
  const anonymous = viewers.length - named.length;
  const tail = anonymous === 0 ? "" : ` and ${anonymous} other${anonymous === 1 ? "" : "s"}`;
  const verb = viewers.length === 1 ? "has" : "have";
  if (named.length === 0) {
    return `${viewers.length} ${viewers.length === 1 ? "person has" : "people have"} this chat open`;
  }
  return `${named.join(", ")}${tail} ${verb} this chat open`;
}

/** The roster of a chat, held open for as long as the component is mounted,
 *  with this socket's own peer id so this tab can be taken out of it. */
export function useChatViewers(chatId: string | undefined): { peers: PresencePeer[]; peerId: string | null } {
  const { client, peerId } = useRealtimePeer(Boolean(chatId));
  const peers = usePresence(client, chatId ? channelOf("chat", chatId) : null);
  return { peers, peerId };
}

export function ChatPresence({ chatId }: { chatId: string | undefined }): ReactElement | null {
  const me = useCurrentUser();
  const roster = useChatViewers(chatId);
  const viewers = viewersOf(roster.peers, { peerId: roster.peerId });
  // Who the reader is names their own other window; it never decides who is on
  // the row, which is the peer id's job alone.
  const selfUserId = me.data?.id ?? null;
  // Alone in the chat: the stack has nothing to say, so it says nothing.
  if (viewers.length === 0) return null;
  const faces = viewers.slice(0, MAX_FACES);
  const hidden = viewers
    .slice(MAX_FACES)
    .map((v) => faceTitle(v, selfUserId));
  return (
    <div className="chat-presence" role="group" aria-label={presenceLabel(viewers)}>
      {faces.map((viewer) => {
        const title = faceTitle(viewer, selfUserId);
        return (
          <Tooltip key={viewer.userId} label={title} side="bottom" wrap>
            {(tip) => (
              <Avatar
                {...tip}
                className="chat-presence__face"
                data-user-id={viewer.userId}
                label={title}
                tabIndex={0}
                hue={hueOf(viewer)}
                picture={viewer.avatarUrl}
                initials={initialsOf(viewer.name)}
              />
            )}
          </Tooltip>
        );
      })}
      {hidden.length > 0 ? (
        <Tooltip
          label={hidden.map((name, i) => (
            <span key={i} className="chat-presence__tip-line">
              {name}
            </span>
          ))}
          side="bottom"
          wrap
        >
          {(tip) => (
            <span
              {...tip}
              className="chat-presence__face chat-presence__more"
              role="img"
              aria-label={hidden.join(", ")}
              tabIndex={0}
            >
              +{hidden.length}
            </span>
          )}
        </Tooltip>
      ) : null}
    </div>
  );
}

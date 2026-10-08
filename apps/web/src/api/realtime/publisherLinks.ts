// Whether the box publishing a chat is still on the line, as the server last said.
//
// A killed box went unnoticed for most of a minute: the machine's status turns
// only when its heartbeat lapses past the ready window. The server tells every
// reader of a chat the moment the box's socket goes (`publisher` `gone`) and
// when it is back on the channel (`here`), so the page can say "reconnecting"
// straight away. `gone` is a hint, not a verdict: `here` withdraws it, a `gone`
// some replica noticed late never outranks a `here` sent after it (the server's
// clock rides each frame), and a hint older than the heartbeat's own window
// lapses, because by then the machine's status says what is true.

import { useEffect, useState } from "react";
import { create } from "zustand";

import { keys } from "../keys";
import { queryClient } from "../queryClient";
import type { ServerFrame } from "./wsClient";

/** How long a `gone` stands before the machine's own status takes over: the
 *  ready window (four missed fifteen-second heartbeats) plus one sweep. */
export const PUBLISHER_GONE_HINT_MS = 75_000;

const CHAT_CHANNEL = /^doc:chat:(.+)$/;

interface PublisherLink {
  state: "here" | "gone";
  /** The server's clock when it said so, in milliseconds. */
  at: number;
  /** This tab's clock when it heard, for the lapse. */
  heard: number;
}

interface PublisherLinks {
  byChat: Record<string, PublisherLink>;
}

export const usePublisherLinks = create<PublisherLinks>(() => ({ byChat: {} }));

/** Hear one frame. Exposed for the socket's listener and for tests. */
export function notePublisherFrame(frame: ServerFrame, now: () => number = Date.now): void {
  if (frame.t !== "publisher") return;
  const chatId = CHAT_CHANNEL.exec(frame.channel)?.[1];
  if (chatId === undefined) return;
  const at = Date.parse(frame.at);
  if (Number.isNaN(at)) return;
  const known = usePublisherLinks.getState().byChat[chatId];
  if (known !== undefined && known.at > at) return;
  usePublisherLinks.setState((state) => ({
    byChat: { ...state.byChat, [chatId]: { state: frame.state, at, heard: now() } },
  }));
  // One read of the machine, now: its status may already say more than the
  // hint does, and nothing else would ask before the next poll.
  void queryClient.invalidateQueries({ queryKey: keys.machines.current, exact: true });
}

/** Listen on a socket for the server's word about publishers. */
export function watchPublishers(client: {
  onFrame: (listener: (frame: ServerFrame) => void) => () => void;
}): () => void {
  return client.onFrame((frame) => notePublisherFrame(frame));
}

/** Whether the box publishing `chatId` is known to have gone, and the hint has
 *  not lapsed. Re-renders the caller when it lapses. */
export function usePublisherGone(chatId: string | undefined): boolean {
  const link = usePublisherLinks((state) => (chatId ? state.byChat[chatId] : undefined));
  const lapsesAt = link?.state === "gone" ? link.heard + PUBLISHER_GONE_HINT_MS : null;
  const [, setLapsed] = useState(0);
  useEffect(() => {
    if (lapsesAt === null) return;
    const timer = setTimeout(() => setLapsed((n) => n + 1), Math.max(0, lapsesAt - Date.now()));
    return () => clearTimeout(timer);
  }, [lapsesAt]);
  return lapsesAt !== null && Date.now() < lapsesAt;
}

/** Forget everything heard: a test's `beforeEach`, or a sign-out. */
export function resetPublisherLinks(): void {
  usePublisherLinks.setState({ byChat: {} });
}

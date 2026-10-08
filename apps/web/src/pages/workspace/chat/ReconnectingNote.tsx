// What a chat says while the browser has no network.
//
// A chat is the one surface in the portal that is expected to sit still for
// minutes at a time: the reader has sent something and is watching an answer
// arrive a token at a time. So a connection that drops looks exactly like a
// model that is thinking, and the reader waits — sometimes for a very long
// time — on an answer that is never coming.
//
// The note says which of the two it is, and nothing else. It is not a toast:
// a toast is a thing to dismiss, it covers the transcript, and this state is
// not an event but a condition that ends on its own. It waits out a delay
// first, because a two-second drop while a laptop hops access points is a
// condition the reader never needed to know about, and a note that flickers on
// every hop is noise they learn to stop reading.

import { useEffect, useState, type ReactElement } from "react";

import { browserOnline, onBrowserOnline } from "../../../lib/online";

/** How long the browser must be offline before it is worth saying so. Short
 *  enough that a reader watching a stalled answer is not left guessing, long
 *  enough that a hop between access points passes unremarked. */
export const RECONNECTING_AFTER_MS = 5_000;

export const RECONNECTING_NOTE = "Reconnecting…";

/** Whether the browser has been offline for at least `afterMs`.
 *
 *  Reads `navigator.onLine` for the state it starts in and follows the two
 *  events for every change after — a tab opened while the network was already
 *  down is as offline as one that drops a minute in. Coming back online clears
 *  the wait as well as the note, so a drop that resolves inside the delay is
 *  never announced at all. */
export function useOfflineFor(afterMs: number = RECONNECTING_AFTER_MS): boolean {
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const clear = (): void => {
      if (timer !== undefined) clearTimeout(timer);
      timer = undefined;
    };
    const went = (): void => {
      clear();
      timer = setTimeout(() => setOffline(true), afterMs);
    };
    const came = (): void => {
      clear();
      setOffline(false);
    };
    if (!browserOnline()) went();
    const unsubscribe = onBrowserOnline({ offline: went, online: came });
    return () => {
      clear();
      unsubscribe();
    };
  }, [afterMs]);
  return offline;
}

/** The line itself, above the composer. Nothing at all while the browser is
 *  connected — including no reserved space, so a chat that never drops is not
 *  paying for the note in layout. */
export function ReconnectingNote(): ReactElement | null {
  const offline = useOfflineFor();
  if (!offline) return null;
  return (
    <p className="chat-dock__reconnecting" role="status">
      {RECONNECTING_NOTE}
    </p>
  );
}

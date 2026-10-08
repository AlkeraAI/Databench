// The chat page's heartbeat for the chat warmed ahead of the reader's first
// message.
//
// While this page is open and its tab visible, the server keeps one chat
// warmed for the reader: placed, its folder leased, its agent session opened
// by the box, hidden from every list until the empty composer's first send
// claims it. The heartbeat is what "on the page" means: one call when the page
// mounts or the tab comes back into view, one every minute while it stays
// visible, and nothing while it is hidden — the server reaps a spare whose
// heartbeat has stopped. A refused or failed call is swallowed: the spare is
// an optimisation, and the first message takes the ordinary path without it.

import { useEffect } from "react";

import { warmSpare } from "../../../api/cloudChat/transport";

/** How often the page says it is still here. The server's idle cutoff is
 *  three of these, so a slow network never reaps a spare on its own. */
export const SPARE_HEARTBEAT_MS = 60_000;

// One beat at a time for this page. StrictMode mounts the effect, tears it
// down and mounts it again, so a bare beat-on-mount fires twice in the same
// tick; the tab coming back while a beat is still out does the same. A second
// beat on top of one in flight says nothing the first has not, so it joins it.
// Two *tabs* still race — that is the server's lock to hold, not this one's.
let inFlight: Promise<void> | null = null;

function beatOnce(): void {
  if (inFlight !== null) return;
  inFlight = warmSpare()
    .then(() => undefined)
    .catch(() => undefined)
    .finally(() => {
      inFlight = null;
    });
}

export function useSpareWarmer(enabled: boolean): void {
  useEffect(() => {
    if (!enabled || typeof document === "undefined") return;
    let timer: ReturnType<typeof setInterval> | null = null;
    const beat = (): void => {
      beatOnce();
    };
    const start = (): void => {
      if (timer !== null) return;
      beat();
      timer = setInterval(beat, SPARE_HEARTBEAT_MS);
    };
    const stop = (): void => {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
    };
    const onVisibility = (): void => {
      if (document.visibilityState === "visible") start();
      else stop();
    };
    document.addEventListener("visibilitychange", onVisibility);
    onVisibility();
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
    };
  }, [enabled]);
}

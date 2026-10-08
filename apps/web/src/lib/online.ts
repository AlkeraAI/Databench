// Whether the browser says it is online.
//
// `navigator.onLine` is a coarse signal: false means the machine has no
// network at all, true means only that it has some. It is still the one fact a
// tab learns the instant the network goes, long before a stalled stream or a
// failed request says so, and the `online` event is the moment worth asking
// again. A browser without the property reads as online.

import { useSyncExternalStore } from "react";

export function browserOnline(): boolean {
  return typeof navigator === "undefined" || navigator.onLine !== false;
}

/** What to do when the browser says the network went, or came back. */
export interface OnlineHandlers {
  readonly online?: () => void;
  readonly offline?: () => void;
}

/** Follow the browser's two network events until the returned function is
 *  called. Only the handlers given are listened for, so a caller that only
 *  wants "the network is back, try now" registers nothing for the drop. */
export function onBrowserOnline({ online, offline }: OnlineHandlers): () => void {
  if (online) window.addEventListener("online", online);
  if (offline) window.addEventListener("offline", offline);
  return () => {
    if (online) window.removeEventListener("online", online);
    if (offline) window.removeEventListener("offline", offline);
  };
}

function subscribe(onChange: () => void): () => void {
  return onBrowserOnline({ online: onChange, offline: onChange });
}

/** The React form of {@link browserOnline}; re-renders the caller when it flips. */
export function useBrowserOnline(): boolean {
  return useSyncExternalStore(subscribe, browserOnline, () => true);
}

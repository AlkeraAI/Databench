// The portal's one realtime socket, opened on demand.
//
// Every live document and presence roster shares a single connection, created the first time
// something asks for it and started while at least one caller holds it. When the last caller
// lets go the socket lingers for half a minute before closing, so a reader who flips between
// two entries does not pay a fresh ticket and handshake for each.

import { fireUnauthorized } from "../client";
import { useRealtimeStatus } from "../events/status";
import { watchPublishers } from "./publisherLinks";
import { WsClient } from "./wsClient";
import { REALTIME_LINGER_MS } from "@/lib/limits";

export const REALTIME_CLIENT_LINGER_MS = REALTIME_LINGER_MS;

let instance: WsClient | null = null;
let holders = 0;
let lingerTimer: ReturnType<typeof setTimeout> | null = null;

function build(): WsClient {
  const client = new WsClient({
    onStatus: (status) => useRealtimeStatus.getState().setWs(status),
    onUnauthorized: fireUnauthorized,
  });
  // For the life of the client: the server's word on publishers is about the
  // chats this socket holds, whoever on the page subscribed them.
  watchPublishers(client);
  return client;
}

/** The shared client, created on first use. It is not started until acquired. */
export function getRealtimeClient(): WsClient {
  if (instance === null) instance = build();
  return instance;
}

/** Hold the shared client open. Starts it on the first hold; the returned release lets go,
 *  and the socket closes {@link REALTIME_CLIENT_LINGER_MS} after the last release. */
export function acquireRealtimeClient(): () => void {
  const client = getRealtimeClient();
  holders += 1;
  if (lingerTimer !== null) {
    clearTimeout(lingerTimer);
    lingerTimer = null;
  }
  client.start();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    holders -= 1;
    if (holders > 0) return;
    lingerTimer = setTimeout(() => {
      lingerTimer = null;
      if (holders === 0) instance?.stop();
    }, REALTIME_CLIENT_LINGER_MS);
  };
}

/** Swap the shared client for a test double (or null to forget it). Stops the one it replaces. */
export function setRealtimeClientForTests(client: WsClient | null): void {
  if (lingerTimer !== null) {
    clearTimeout(lingerTimer);
    lingerTimer = null;
  }
  instance?.stop();
  instance = client;
  holders = 0;
}

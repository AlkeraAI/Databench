// Telling this browser's other tabs that the session changed.
//
// A switch of org, leaving one, or a sign-out in one tab changes the session every tab of the
// browser shares. The other tabs hear it here and reload, so none of them keeps
// showing the org that was left. This is a courtesy, not the guard: a browser
// without BroadcastChannel (or a tab that misses the message) still meets the
// server's 409 on its next request, which reloads it the same way.
//
// One channel per tab, used for both posting and listening: a BroadcastChannel
// never delivers a message to the instance that posted it, so a tab does not
// reload itself on its own announcement.

/** What a tab announces. */
export type SessionMessage =
  | { type: "org_switched"; org_team_id: string }
  | { type: "logged_out" }
  /** The person left the org the session was in and is in none now. */
  | { type: "org_left" };

export const SESSION_CHANNEL_NAME = "alkera-session";

/** The part of BroadcastChannel this module uses, so a test can hand in a fake. */
export interface SessionChannelLike {
  postMessage(message: unknown): void;
  addEventListener(type: "message", listener: (event: MessageEvent) => void): void;
  removeEventListener(type: "message", listener: (event: MessageEvent) => void): void;
}

export type SessionChannelFactory = (name: string) => SessionChannelLike;

function defaultFactory(): SessionChannelFactory | null {
  const Ctor = (globalThis as { BroadcastChannel?: new (name: string) => SessionChannelLike })
    .BroadcastChannel;
  return Ctor ? (name) => new Ctor(name) : null;
}

let factory: SessionChannelFactory | null | undefined;
let channel: SessionChannelLike | null = null;

/** Replace how the channel is opened (tests), or pass null for a browser
 *  without one. Drops the channel already open. */
export function setSessionChannelFactory(next: SessionChannelFactory | null | undefined): void {
  factory = next;
  channel = null;
}

function open(): SessionChannelLike | null {
  if (channel) return channel;
  const make = factory === undefined ? defaultFactory() : factory;
  if (!make) return null;
  try {
    channel = make(SESSION_CHANNEL_NAME);
  } catch {
    channel = null;
  }
  return channel;
}

/** Tell the other tabs. A browser without a channel tells nobody, quietly. */
export function announceSession(message: SessionMessage): void {
  try {
    open()?.postMessage(message);
  } catch {
    // The other tabs learn from the server's 409 instead.
  }
}

function isSessionMessage(data: unknown): data is SessionMessage {
  if (typeof data !== "object" || data === null) return false;
  const type = (data as { type?: unknown }).type;
  return type === "org_switched" || type === "logged_out" || type === "org_left";
}

/** Listen for another tab's announcement. Returns the unsubscribe. */
export function onSessionMessage(listener: (message: SessionMessage) => void): () => void {
  const ch = open();
  if (!ch) return () => undefined;
  const handle = (event: MessageEvent) => {
    if (isSessionMessage(event.data)) listener(event.data);
  };
  ch.addEventListener("message", handle);
  return () => ch.removeEventListener("message", handle);
}

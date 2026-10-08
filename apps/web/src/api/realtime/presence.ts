// Who else is on a channel.
//
// The server keeps presence per (channel, peer) with a TTL, fans a roster out on subscribe
// and a delta on every join, leave and heartbeat, and heartbeats a socket's joined channels
// itself on every keepalive tick. The client still heartbeats on its own cadence: it costs
// one small frame and keeps the roster honest through a server whose tick is delayed.
//
// A roster also forgets a peer it has not heard from for the TTL the server named in its
// welcome. The server announces the peers its sweep removes, so this is the backstop for a
// leave that never arrived: without it a peer whose process died stayed on screen, caret and
// all, until the reader reloaded.

import { useEffect, useState } from "react";

import { acquireRealtimeClient, getRealtimeClient } from "./client";
import type { PresenceCursor, PresencePeer, ServerFrame, WsClient } from "./wsClient";
import { PRESENCE_CURSOR_THROTTLE_MS, PRESENCE_EXPIRY_CHECKS_PER_TTL, PRESENCE_HEARTBEAT_MS } from "@/lib/limits";

export type { PresenceCursor, PresencePeer } from "./wsClient";

export const DEFAULT_PRESENCE_HEARTBEAT_MS = PRESENCE_HEARTBEAT_MS;

export interface JoinPresenceOptions {
  heartbeatMs?: number;
  timers?: { setTimeout: (fn: () => void, ms: number) => unknown; clearTimeout: (handle: unknown) => void };
}

const DEFAULT_TIMERS = {
  setTimeout: (fn: () => void, ms: number): unknown => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle: unknown): void => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

function sorted(peers: Map<string, PresencePeer>): PresencePeer[] {
  return [...peers.values()].sort((a, b) => a.peer_id.localeCompare(b.peer_id));
}

/** What a peer with no resolved name and no address is called. */
export const ANONYMOUS_VIEWER_NAME = "Someone";

/** What the reader's OWN second window is called. Their name repeated at them
 *  would read as a colleague who happens to share it. */
export const OWN_WINDOW_NAME = "You (another window)";

/** What a viewer is called wherever one is drawn — a face, a caret, a strip:
 *  their display name, their login address where the server resolved no name,
 *  and "Someone" where it resolved neither. */
export function viewerName(
  who: { userId: string; name: string; email: string },
  selfUserId: string | null = null,
): string {
  if (selfUserId !== null && who.userId === selfUserId) return OWN_WINDOW_NAME;
  return who.name.trim() || who.email.trim() || ANONYMOUS_VIEWER_NAME;
}

/** One distinct person on a roster, other than this tab. */
export interface PresenceViewer {
  userId: string;
  /** Their display name; empty where the server resolved none. */
  name: string;
  avatarUrl: string | null;
  /** Their login address where the server resolved one; what their colour is
   *  keyed off. Empty falls the colour back to the user id. */
  email: string;
}

/** A person, and the places (one per roster that carried them) they are in. */
export interface PlacedViewer extends PresenceViewer {
  places: string[];
}

/** The people behind several rosters, one entry per person in the order they
 *  were first met, each with the places they were seen in. A roster filed under
 *  a `null` place adds a person without adding a place.
 *
 *  A tab is what presence counts, so what drops out is this socket and only
 *  this socket: a second window of the reader's own is another place the
 *  document is open, and it is counted — as the person, once, however many
 *  tabs they have. Passing a peer id no roster carries (a socket that has not
 *  been welcomed yet) subtracts nothing rather than guessing. A person's name,
 *  address and picture are taken from whichever of their peers carries one, so
 *  a socket that joined before the server resolved names does not blank out a
 *  person their other tab named. */
export function viewersAcross(
  rosters: Iterable<readonly [place: string | null, peers: readonly PresencePeer[]]>,
  selfPeerId: string | null,
): PlacedViewer[] {
  const byUser = new Map<string, PlacedViewer>();
  for (const [place, peers] of rosters) {
    for (const peer of peers) {
      if (peer.peer_id === selfPeerId) continue;
      const name = (peer.display_name ?? "").trim();
      const email = (peer.email ?? "").trim();
      const avatarUrl = peer.avatar_url ?? null;
      const held = byUser.get(peer.user_id);
      if (held === undefined) {
        byUser.set(peer.user_id, { userId: peer.user_id, name, email, avatarUrl, places: place === null ? [] : [place] });
        continue;
      }
      if (held.name === "" && name !== "") held.name = name;
      if (held.email === "" && email !== "") held.email = email;
      if (held.avatarUrl === null && avatarUrl !== null) held.avatarUrl = avatarUrl;
      if (place !== null && !held.places.includes(place)) held.places.push(place);
    }
  }
  return [...byUser.values()];
}

/** The people behind one roster, in roster order, without this tab. */
export function rosterViewers(peers: readonly PresencePeer[], selfPeerId: string | null): PresenceViewer[] {
  return viewersAcross([[null, peers]], selfPeerId).map(({ userId, name, avatarUrl, email }) => ({
    userId,
    name,
    avatarUrl,
    email,
  }));
}

/** The people watching a channel besides this tab, named once each, in roster
 *  order — and, by its length, how many of them there are. */
export function viewerNames(
  peers: readonly PresencePeer[],
  selfPeerId: string | null,
  selfUserId: string | null = null,
): string[] {
  return rosterViewers(peers, selfPeerId).map((who) => viewerName(who, selfUserId));
}

/** The two letters a face carries: the initials of a two-part name, or the
 *  first two characters of a one-part one. */
export function initialsOf(name: string): string {
  const parts = name.split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "";
  if (parts.length === 1) return (parts[0] ?? "").slice(0, 2);
  return `${parts[0]?.[0] ?? ""}${parts[parts.length - 1]?.[0] ?? ""}`;
}

/** What hovering a face says, and what the face is called to a reader who
 *  never sees it: the person's name, their address where no name was resolved,
 *  "You" for the reader's own other window, and "Someone" where the server
 *  resolved nothing. */
export function faceTitle(viewer: PresenceViewer, selfUserId: string | null = null): string {
  const known = viewer.name.trim() || viewer.email.trim();
  if (known !== "") return known;
  const own = selfUserId !== null && viewer.userId === selfUserId;
  return own ? "You" : viewerName(viewer, selfUserId);
}

/** Hold the realtime socket while `active`, and hand it out with the id the
 *  server minted for it, which is how this tab is told apart on a roster. The
 *  id is re-read when the socket (re)opens, since a reconnect mints a new one. */
export function useRealtimePeer(active: boolean): { client: WsClient | null; peerId: string | null } {
  const [client, setClient] = useState<WsClient | null>(null);
  const [, setOpens] = useState(0);
  useEffect(() => {
    if (!active) {
      setClient(null);
      return;
    }
    const release = acquireRealtimeClient();
    setClient(getRealtimeClient());
    return () => {
      setClient(null);
      release();
    };
  }, [active]);
  useEffect(() => {
    if (client === null) return;
    return client.onOpen(() => setOpens((n) => n + 1));
  }, [client]);
  return { client, peerId: client?.peerId ?? null };
}

type PresenceFrameOf = Extract<ServerFrame, { t: "presence" }>;
type Timers = NonNullable<JoinPresenceOptions["timers"]>;

/** A roster held by peer id that drops a peer silent for longer than the server's presence
 *  TTL. `onChange` is told the roster after every frame and every expiry that changed it. */
class ExpiringRoster {
  readonly peers = new Map<string, PresencePeer>();
  private readonly heard = new Map<string, number>();
  private sweep: unknown = null;

  constructor(
    private readonly client: WsClient,
    private readonly timers: Timers,
    private readonly onChange: (peers: PresencePeer[]) => void,
  ) {}

  apply(frame: PresenceFrameOf): void {
    applyPresenceFrame(this.peers, frame);
    const now = Date.now();
    if (frame.event === "roster") this.heard.clear();
    for (const peer of frame.peers) {
      if (this.peers.has(peer.peer_id)) this.heard.set(peer.peer_id, now);
      else this.heard.delete(peer.peer_id);
    }
    this.onChange(sorted(this.peers));
    this.arm();
  }

  /** Forget everyone (the socket dropped): says so only when somebody was there. */
  clear(): void {
    this.stop();
    this.heard.clear();
    if (this.peers.size === 0) return;
    this.peers.clear();
    this.onChange([]);
  }

  stop(): void {
    if (this.sweep !== null) {
      this.timers.clearTimeout(this.sweep);
      this.sweep = null;
    }
  }

  private ttlMs(): number | null {
    const seconds = this.client.limits?.presence_ttl_seconds ?? null;
    return seconds === null ? null : seconds * 1000;
  }

  private arm(): void {
    const ttl = this.ttlMs();
    if (this.sweep !== null || ttl === null || this.peers.size === 0) return;
    this.sweep = this.timers.setTimeout(() => {
      this.sweep = null;
      this.expire(ttl);
      this.arm();
    }, ttl / PRESENCE_EXPIRY_CHECKS_PER_TTL);
  }

  private expire(ttl: number): void {
    const cutoff = Date.now() - ttl;
    let dropped = false;
    for (const [peerId, at] of this.heard) {
      if (at > cutoff) continue;
      this.heard.delete(peerId);
      this.peers.delete(peerId);
      dropped = true;
    }
    if (dropped) this.onChange(sorted(this.peers));
  }
}

/** Fold one presence frame into a roster held by peer id. */
function applyPresenceFrame(peers: Map<string, PresencePeer>, frame: PresenceFrameOf): void {
  switch (frame.event) {
    case "roster":
      peers.clear();
      for (const peer of frame.peers) peers.set(peer.peer_id, peer);
      break;
    case "join":
    case "heartbeat":
      // A liveness delta carries no caret; the one the peer last reported
      // stays where it was rather than blinking out on every heartbeat.
      for (const peer of frame.peers) {
        peers.set(peer.peer_id, { ...peer, cursor: peer.cursor ?? peers.get(peer.peer_id)?.cursor ?? null });
      }
      break;
    case "cursor":
      // A caret belongs to a face: a peer the roster has not been told
      // about has none, and its caret would float unowned until it joined.
      for (const peer of frame.peers) {
        const held = peers.get(peer.peer_id);
        if (held) peers.set(peer.peer_id, { ...held, cursor: peer.cursor ?? null });
      }
      break;
    case "leave":
      for (const peer of frame.peers) peers.delete(peer.peer_id);
      break;
  }
}

/**
 * Watch `channel`'s roster WITHOUT joining it: the socket subscribes, the server answers with
 * the roster and every delta after it, and this tab never appears on it. For a surface that
 * shows who is in several documents at once (a workspace's chats) without claiming to have
 * each of them open.
 *
 * The subscription is ref-counted with every other holder of the channel, so a channel this
 * tab already holds would send no second `subscribe` and no roster. An explicit `subscribe`
 * is sent once the socket is open to ask for one; the server answers a repeat with the same
 * roster and changes nothing else.
 */
export function observePresence(
  client: WsClient,
  channel: string,
  onPeers: (peers: PresencePeer[]) => void,
  opts: Pick<JoinPresenceOptions, "timers"> = {},
): () => void {
  const roster = new ExpiringRoster(client, opts.timers ?? DEFAULT_TIMERS, onPeers);
  let released = false;
  const onFrame = (frame: ServerFrame): void => {
    if (released || frame.t !== "presence" || frame.channel !== channel) return;
    roster.apply(frame);
  };
  const onClose = (): void => {
    if (released) return;
    roster.clear();
  };
  const offFrame = client.onFrame(onFrame);
  const offClose = client.onClose(onClose);
  const release = client.subscribe(channel);
  if (client.peerId !== null) client.send({ t: "subscribe", channel });
  return () => {
    if (released) return;
    released = true;
    roster.stop();
    offFrame();
    offClose();
    release();
  };
}

/**
 * Join `channel`'s presence for as long as the returned release is not called: the join is
 * sent once the server confirms the subscription (and again after every reconnect), a
 * heartbeat rides every `heartbeatMs`, a leave is sent on release, and `onPeers` receives the
 * roster after every change. A dropped socket empties the roster until the next one arrives.
 */
export function joinPresence(
  client: WsClient,
  channel: string,
  onPeers: (peers: PresencePeer[]) => void,
  opts: JoinPresenceOptions = {},
): () => void {
  const heartbeatMs = opts.heartbeatMs ?? DEFAULT_PRESENCE_HEARTBEAT_MS;
  const timers = opts.timers ?? DEFAULT_TIMERS;
  const roster = new ExpiringRoster(client, timers, onPeers);
  let timer: unknown = null;
  let released = false;

  const stopHeartbeat = (): void => {
    if (timer !== null) {
      timers.clearTimeout(timer);
      timer = null;
    }
  };
  const startHeartbeat = (): void => {
    stopHeartbeat();
    const tick = (): void => {
      timer = null;
      if (released) return;
      client.send({ t: "presence.heartbeat", channel });
      timer = timers.setTimeout(tick, heartbeatMs);
    };
    timer = timers.setTimeout(tick, heartbeatMs);
  };

  const onFrame = (frame: ServerFrame): void => {
    if (released) return;
    if (frame.t === "subscribed" && frame.channel === channel) {
      client.send({ t: "presence.join", channel });
      startHeartbeat();
      return;
    }
    if (frame.t !== "presence" || frame.channel !== channel) return;
    roster.apply(frame);
  };
  const onClose = (): void => {
    if (released) return;
    stopHeartbeat();
    roster.clear();
  };

  const offFrame = client.onFrame(onFrame);
  const offClose = client.onClose(onClose);
  const release = client.subscribe(channel);

  return () => {
    if (released) return;
    released = true;
    stopHeartbeat();
    roster.stop();
    offFrame();
    offClose();
    client.send({ t: "presence.leave", channel });
    release();
  };
}

interface SharedJoin {
  peers: PresencePeer[];
  listeners: Set<(peers: PresencePeer[]) => void>;
  release: () => void;
}
const shared = new WeakMap<WsClient, Map<string, SharedJoin>>();

/**
 * One presence join per (socket, channel), however many surfaces want the roster.
 *
 * The faces at the top of a chat and the carets in its composer are two places
 * in the tree asking the same question. Two independent joins would each send a
 * `presence.leave` on unmount, and the first one out would take this peer off
 * everybody else's roster while the other was still watching — the reader would
 * vanish from the room without leaving it. So the join is held once and fanned
 * out, and only the last release leaves. The first subscriber's options are the
 * ones the held join runs under; a second subscriber does not restart it.
 */
function shareJoin(
  client: WsClient,
  channel: string,
  onPeers: (peers: PresencePeer[]) => void,
  opts: JoinPresenceOptions,
): () => void {
  let byChannel = shared.get(client);
  if (byChannel === undefined) {
    byChannel = new Map();
    shared.set(client, byChannel);
  }
  const channels = byChannel;
  let entry = channels.get(channel);
  if (entry === undefined) {
    const held: SharedJoin = { peers: [], listeners: new Set(), release: () => undefined };
    held.release = joinPresence(
      client,
      channel,
      (peers) => {
        held.peers = peers;
        for (const listener of [...held.listeners]) listener(peers);
      },
      opts,
    );
    entry = held;
    channels.set(channel, held);
  }
  const join = entry;
  join.listeners.add(onPeers);
  // What the roster already says, for a surface that mounted after the join.
  if (join.peers.length > 0) onPeers(join.peers);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    join.listeners.delete(onPeers);
    if (join.listeners.size > 0) return;
    channels.delete(channel);
    join.release();
  };
}

/** The roster of `channel` while mounted; empty when `channel` is null or the socket is down. */
export function usePresence(client: WsClient | null, channel: string | null, opts?: JoinPresenceOptions): PresencePeer[] {
  const [peers, setPeers] = useState<PresencePeer[]>([]);
  const heartbeatMs = opts?.heartbeatMs;
  const timers = opts?.timers;
  useEffect(() => {
    if (client === null || channel === null) {
      setPeers([]);
      return;
    }
    const release = shareJoin(client, channel, setPeers, { heartbeatMs, timers });
    return () => {
      release();
      setPeers([]);
    };
  }, [client, channel, heartbeatMs, timers]);
  return peers;
}

export const DEFAULT_CURSOR_THROTTLE_MS = PRESENCE_CURSOR_THROTTLE_MS;

export interface CursorSenderOptions {
  /** The floor between two caret frames. Below the socket's own storm cap
   *  (200 frames per ten seconds, shared with heartbeats and pings), which a
   *  keystroke-rate stream would otherwise trip. */
  throttleMs?: number;
  timers?: JoinPresenceOptions["timers"];
}

/**
 * Report where this peer's caret is, at most once per `throttleMs`: the first
 * move goes at once, moves inside the window are folded into one frame
 * carrying the LATEST position when the window closes, and an unchanged
 * position is never re-sent. `stop` drops whatever is pending.
 */
export function cursorSender(
  client: WsClient,
  channel: string,
  opts: CursorSenderOptions = {},
): { move: (cursor: PresenceCursor) => void; stop: () => void } {
  const throttleMs = opts.throttleMs ?? DEFAULT_CURSOR_THROTTLE_MS;
  const timers = opts.timers ?? DEFAULT_TIMERS;
  let sent: PresenceCursor | null = null;
  let pending: PresenceCursor | null = null;
  let timer: unknown = null;
  const same = (a: PresenceCursor | null, b: PresenceCursor): boolean =>
    a !== null && a.offset === b.offset && a.anchor === b.anchor && a.before === b.before && a.after === b.after;
  const flush = (): void => {
    timer = null;
    const next = pending;
    pending = null;
    if (next === null || same(sent, next)) return;
    if (client.send({ t: "presence.cursor", channel, cursor: next })) sent = next;
    timer = timers.setTimeout(flush, throttleMs);
  };
  return {
    move(cursor) {
      if (same(sent, cursor) && pending === null) return;
      pending = cursor;
      if (timer === null) flush();
    },
    stop() {
      if (timer !== null) {
        timers.clearTimeout(timer);
        timer = null;
      }
      pending = null;
    },
  };
}

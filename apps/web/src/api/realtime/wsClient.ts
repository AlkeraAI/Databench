// The realtime socket client: one connection to `WS /api/v1/ws`, shared by every live
// document and presence roster on the page.
//
// The handshake (see packages/api-core/alkera_core/schemas/realtime/README.md): mint a
// single-use, thirty-second ticket with `POST /api/v1/ws/tickets` (the session cookie
// authenticates that call, as it does every other), then open the socket offering two
// subprotocols — `alkera-v1` and `alkera-ticket.<ticket>`. The ticket therefore travels in
// the `Sec-WebSocket-Protocol` header and never in a URL an access log would keep; the
// server accepts nothing else. Every reconnect mints a fresh ticket, because a ticket is
// burned on first use on any replica.
//
// After the server's `welcome` (which carries the peer id every envelope this socket sends
// must name) the client re-subscribes each channel a caller holds a reference to, so a
// reconnect is invisible to the documents above it: they see `subscribed` again and
// re-`hello`. Close codes are acted on the way the README asks: 4401 mints once more and then
// backs off, 4403 stops (a misconfigured origin), 4408 mints and reconnects at once, 4413 logs
// and reconnects, 4503 backs off onto a replica that can deliver, everything else backs off on
// the same floor-and-cap schedule the event stream uses.

import type { components } from "@alkera/sdk";

import { api, apiBaseUrl, request } from "../client";
import { ApiError } from "../errors";
import {
  DEFAULT_DOWN_AFTER_MS,
  DEFAULT_SSE_BACKOFF,
  reconnectDelayMs,
  type SseBackoff,
  type SseTimers,
} from "../events/sseClient";
import type { RealtimeStatus } from "../events/status";
import { onMinClientGeneration } from "./clientGeneration";
import { WS_HEARTBEAT_MS, WS_PONG_TIMEOUT_MS, WS_STABLE_AFTER_MS } from "@/lib/limits";

export type WsTicket = components["schemas"]["WsTicketResponse"];
export type RealtimeProtocolDescriptor = components["schemas"]["RealtimeProtocolDescriptor"];
export type EnvelopeKind = RealtimeProtocolDescriptor["envelope_kinds"][number];
export type DocType = RealtimeProtocolDescriptor["doc_types"][number];
export type OpIntent = RealtimeProtocolDescriptor["op_intents"][number];

export const WS_SUBPROTOCOL = "alkera-v1";
export const WS_TICKET_SUBPROTOCOL_PREFIX = "alkera-ticket.";

/** The vocabularies this client speaks, spelled once and pinned against `openapi.json` by a
 *  test so the server cannot grow a kind, type or intent the client silently misreads. */
export const ENVELOPE_KINDS: readonly EnvelopeKind[] = [
  "hello",
  "snapshot",
  "op",
  "ack",
  "presence",
  "reload",
  "error",
  "crdt",
];
// `chat_workspace` is the draft's spelling before the rename, still on the wire for
// older builds; this client never subscribes under it.
export const DOC_TYPES: readonly DocType[] = ["chat", "artifact", "chat_draft", "file", "notebook", "chat_workspace"];
export const OP_INTENTS: readonly OpIntent[] = ["append", "set_meta", "user_message", "chunk", "set_fields"];

/** The close codes the gateway sends (the 4xxx application range), by the names it serves
 *  them under at `GET /api/v1/ws/protocol`. Pinned against that set by a test: the schema
 *  types this map as a free-form `dict[str, int]`, so a code the server grows would
 *  otherwise reach the client as nothing at all. */
export const CLOSE_CODES = {
  UNAUTHORIZED: 4401,
  ORIGIN_FORBIDDEN: 4403,
  NOT_FOUND: 4404,
  SESSION_EXPIRED: 4408,
  FRAME_TOO_LARGE: 4413,
  TOO_MANY: 4429,
  SERVER_RESET: 4500,
  UNAVAILABLE: 4503,
} as const;

/** The code this client closes with when the server stopped answering pings. */
export const CLOSE_PONG_TIMEOUT = 4000;

export const DEFAULT_HEARTBEAT_MS = WS_HEARTBEAT_MS;
export const DEFAULT_PONG_TIMEOUT_MS = WS_PONG_TIMEOUT_MS;
/** How long a welcomed socket must STAY welcomed before the backoff counter returns to the
 *  floor. A connection that is accepted and dropped again is not a recovery, and treating it
 *  as one is how a fleet turns a brief server wobble into a self-sustaining reconnect storm
 *  (Convex took a service down doing exactly that, 50 q/s to 20,000 q/s). So the counter is
 *  reset by DURATION, never by the handshake. */
export const DEFAULT_STABLE_AFTER_MS = WS_STABLE_AFTER_MS;

/** A doc-sync envelope, exactly as the server spells it. */
export interface Envelope<P = Record<string, unknown>> {
  doc_id: string;
  doc_type: DocType;
  epoch: number;
  peer_id: string;
  seq: number;
  kind: EnvelopeKind;
  payload: P;
}

/** Where a peer's caret is in the shared composer draft: an offset into the
 *  text THEY were looking at, the selection's other end, and a little of the
 *  text either side so a reader whose copy has moved on can put the caret
 *  back beside the words it was at rather than at a stale number. */
export interface PresenceCursor {
  offset: number;
  anchor: number;
  before: string;
  after: string;
}

export interface PresencePeer {
  peer_id: string;
  user_id: string;
  last_seen_at: string;
  /** The peer's caret, on a `cursor` delta; absent on every other frame. */
  cursor?: PresenceCursor | null;
  /** What to call this person on screen, resolved by the SERVER from their user
   *  row. Optional on the wire: an older server does not send it, and a peer
   *  whose user row is gone sends it empty — either way a surface counts the
   *  reader and does not name them, rather than drawing a sliced user id. */
  display_name?: string;
  /** Where the person's picture is, when their profile carries one. Optional on
   *  the wire for the same reason as the name; a face without one draws
   *  initials. */
  avatar_url?: string;
  /** The person's login address, resolved by the SERVER. It is what this
   *  person's COLOUR is keyed off everywhere — a face, a caret — so the same
   *  colleague is the same colour in every chat, tab and deployment. Optional
   *  on the wire (an older server sends none), and a surface then falls back
   *  to the user id. */
  email?: string;
}

export type PresenceEvent = "join" | "leave" | "heartbeat" | "roster" | "cursor";

export type ClientFrame =
  | { t: "subscribe"; channel: string }
  | { t: "unsubscribe"; channel: string }
  | { t: "presence.join"; channel: string }
  | { t: "presence.leave"; channel: string }
  | { t: "presence.heartbeat"; channel: string }
  | { t: "presence.cursor"; channel: string; cursor: PresenceCursor }
  | { t: "ping" }
  | { t: "doc"; envelope: Envelope };

export type ServerFrame =
  | {
      t: "welcome";
      peer_id: string;
      server_time: string;
      instance: string;
      /** The oldest client generation the server serves; 0 from a server that predates it. */
      min_client_generation: number;
      /** The inbound budget this socket is held to; null from a server that predates it. */
      limits: SocketLimits | null;
    }
  | { t: "subscribed"; channel: string; can_write: boolean }
  | { t: "presence"; channel: string; event: PresenceEvent; peers: PresencePeer[] }
  | { t: "reset"; reason: string }
  | { t: "error"; code: string; message: string; channel: string | null }
  | { t: "pong" }
  | { t: "doc"; envelope: Envelope }
  /** The box publishing a chat's document came onto the channel or went from
   *  it; `at` is the server's clock when it said so. */
  | { t: "publisher"; channel: string; state: "here" | "gone"; at: string }
  /** A notebook engine event on `nb:<item_id>` (a join snapshot, then events
   *  in `(kernel_id, seq)` order). The event is read by the notebook's fold. */
  | { t: "nb"; channel: string; event: Record<string, unknown> & { type: string } };

export function channelOf(docType: DocType, docId: string): string {
  return `doc:${docType}:${docId}`;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

/** The caret on a presence peer, or null for anything that is not one — a
 *  newer writer's shape is dropped rather than drawn somewhere wrong. */
export function parsePresenceCursor(value: unknown): PresenceCursor | null {
  if (!isRecord(value)) return null;
  const { offset, anchor } = value;
  if (typeof offset !== "number" || typeof anchor !== "number" || offset < 0 || anchor < 0) return null;
  return {
    offset,
    anchor,
    before: typeof value.before === "string" ? value.before : "",
    after: typeof value.after === "string" ? value.after : "",
  };
}

/** The inbound budget the server holds a socket to: at most `frames_per_window` frames and
 *  `bytes_per_window` bytes in any `window_seconds`, and no frame over `max_frame_bytes`. */
export interface SocketLimits {
  frames_per_window: number;
  bytes_per_window: number;
  window_seconds: number;
  max_frame_bytes: number;
  /** How long a peer stays on a roster unheard from; `null` from a server that does not say. */
  presence_ttl_seconds?: number | null;
}

/** The socket budget in a welcome, or null for anything that is not a usable one. A budget
 *  with a zero or non-finite term would stall a sender that paces itself under it. */
export function parseSocketLimits(value: unknown): SocketLimits | null {
  if (!isRecord(value)) return null;
  const terms = [
    value.frames_per_window,
    value.bytes_per_window,
    value.window_seconds,
    value.max_frame_bytes,
  ];
  if (!terms.every((term) => typeof term === "number" && Number.isFinite(term) && term > 0)) {
    return null;
  }
  const [frames, bytes, window, frame] = terms as number[];
  const ttl = value.presence_ttl_seconds;
  return {
    frames_per_window: frames,
    bytes_per_window: bytes,
    window_seconds: window,
    max_frame_bytes: frame,
    presence_ttl_seconds: typeof ttl === "number" && Number.isFinite(ttl) && ttl > 0 ? ttl : null,
  };
}

export function isEnvelope(value: unknown): value is Envelope {
  if (!isRecord(value)) return false;
  return (
    typeof value.doc_id === "string" &&
    typeof value.doc_type === "string" &&
    (DOC_TYPES as readonly string[]).includes(value.doc_type) &&
    typeof value.epoch === "number" &&
    typeof value.peer_id === "string" &&
    typeof value.seq === "number" &&
    typeof value.kind === "string" &&
    isRecord(value.payload)
  );
}

/** The typed frame in a server message, or null for anything this client does not
 *  understand (a newer server's tag rides through as nothing rather than a throw). */
export function parseServerFrame(raw: unknown): ServerFrame | null {
  if (typeof raw !== "string") return null;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(value) || typeof value.t !== "string") return null;
  switch (value.t) {
    case "welcome":
      return typeof value.peer_id === "string" && value.peer_id !== ""
        ? {
            t: "welcome",
            peer_id: value.peer_id,
            server_time: String(value.server_time ?? ""),
            instance: String(value.instance ?? ""),
            min_client_generation:
              typeof value.min_client_generation === "number" &&
              Number.isInteger(value.min_client_generation) &&
              value.min_client_generation >= 0
                ? value.min_client_generation
                : 0,
            limits: parseSocketLimits(value.limits),
          }
        : null;
    case "subscribed":
      return typeof value.channel === "string"
        ? { t: "subscribed", channel: value.channel, can_write: value.can_write === true }
        : null;
    case "presence": {
      if (typeof value.channel !== "string") return null;
      const event = value.event;
      if (
        event !== "join" &&
        event !== "leave" &&
        event !== "heartbeat" &&
        event !== "roster" &&
        event !== "cursor"
      ) {
        return null;
      }
      const peers = Array.isArray(value.peers)
        ? value.peers.filter(
            (p): p is PresencePeer =>
              isRecord(p) && typeof p.peer_id === "string" && typeof p.user_id === "string",
          )
        : [];
      // The name and the picture ride through when the server sent them, so the
      // roster can say who is reading rather than only how many.
      return { t: "presence", channel: value.channel, event, peers: peers.map((p) => ({
        peer_id: p.peer_id,
        user_id: p.user_id,
        last_seen_at: typeof p.last_seen_at === "string" ? p.last_seen_at : "",
        ...(typeof p.email === "string" && p.email !== "" ? { email: p.email } : {}),
        ...(typeof p.display_name === "string" ? { display_name: p.display_name } : {}),
        ...(typeof p.avatar_url === "string" && p.avatar_url !== "" ? { avatar_url: p.avatar_url } : {}),
        ...(p.cursor === undefined || p.cursor === null ? {} : { cursor: parsePresenceCursor(p.cursor) }),
      })) };
    }
    case "reset":
      return { t: "reset", reason: String(value.reason ?? "") };
    case "error":
      return {
        t: "error",
        code: String(value.code ?? ""),
        message: typeof value.message === "string" ? value.message : "",
        channel: typeof value.channel === "string" ? value.channel : null,
      };
    case "pong":
      return { t: "pong" };
    case "doc":
      return isEnvelope(value.envelope) ? { t: "doc", envelope: value.envelope } : null;
    case "publisher":
      return typeof value.channel === "string" &&
        (value.state === "here" || value.state === "gone") &&
        typeof value.at === "string"
        ? { t: "publisher", channel: value.channel, state: value.state, at: value.at }
        : null;
    case "nb":
      return typeof value.channel === "string" && isRecord(value.event) && typeof value.event.type === "string"
        ? { t: "nb", channel: value.channel, event: value.event as Record<string, unknown> & { type: string } }
        : null;
    default:
      return null;
  }
}

/** The subset of the DOM WebSocket this client drives; a test supplies a fake with the same
 *  shape. Picked from the real type so the production factory needs no adapter or cast. */
export type WebSocketLike = Pick<
  WebSocket,
  "readyState" | "send" | "close" | "onopen" | "onclose" | "onerror" | "onmessage"
>;

export type WebSocketFactory = (url: string, protocols: string[]) => WebSocketLike;

const OPEN = 1;

export interface WsClientOptions {
  /** Default: `POST /api/v1/ws/tickets` through the API client. */
  mintTicket?: () => Promise<WsTicket>;
  /** Default: `apiBaseUrl` with its scheme swapped to ws(s), plus the ticket's `path`. */
  socketUrl?: (path: string) => string;
  /** Default: the browser's WebSocket; null when the environment has none (the client reports down). */
  factory?: WebSocketFactory | null;
  onStatus?: (status: RealtimeStatus) => void;
  onUnauthorized?: () => void;
  /** The server's `min_client_generation` from each `welcome`. Default: reload this tab
   *  into the current build when it is older (see `clientGeneration.ts`). */
  onMinClientGeneration?: (min: number) => void;
  /** Something worth an operator's attention (an origin refusal, an oversized frame). */
  log?: (message: string, detail?: unknown) => void;
  heartbeatMs?: number;
  pongTimeoutMs?: number;
  downAfterMs?: number;
  /** How long a socket must stay welcomed before its reconnect delay returns to the floor. */
  stableAfterMs?: number;
  backoff?: Partial<SseBackoff>;
  timers?: SseTimers;
}

const DEFAULT_TIMERS: SseTimers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

function defaultMintTicket(): Promise<WsTicket> {
  return request(api.POST("/api/v1/ws/tickets"), "could not open the live connection");
}

export function defaultSocketUrl(path: string): string {
  return apiBaseUrl.replace(/^http/i, "ws") + path;
}

const defaultFactory: WebSocketFactory | null =
  typeof WebSocket === "undefined" ? null : (url, protocols) => new WebSocket(url, protocols);

type Listener<T> = (value: T) => void;

export class WsClient {
  private readonly mintTicket: () => Promise<WsTicket>;
  private readonly socketUrl: (path: string) => string;
  private readonly factory: WebSocketFactory | null;
  private readonly onStatus: (status: RealtimeStatus) => void;
  private readonly onUnauthorized: () => void;
  private readonly onMinClientGeneration: (min: number) => void;
  private readonly log: (message: string, detail?: unknown) => void;
  private readonly heartbeatMs: number;
  private readonly pongTimeoutMs: number;
  private readonly downAfterMs: number;
  private readonly stableAfterMs: number;
  private readonly backoff: SseBackoff;
  private readonly timers: SseTimers;

  private _status: RealtimeStatus = "idle";
  private _peerId: string | null = null;
  private _limits: SocketLimits | null = null;
  private socket: WebSocketLike | null = null;
  private stopped = true;
  private generation = 0;
  private attempt = 0;
  private remintedOnce = false;
  private refreshedOnce = false;
  private reconnectTimer: unknown = null;
  private downTimer: unknown = null;
  private heartbeatTimer: unknown = null;
  private pongTimer: unknown = null;
  private stableTimer: unknown = null;
  private readonly refs = new Map<string, number>();
  private readonly frameListeners = new Set<Listener<ServerFrame>>();
  private readonly openListeners = new Set<Listener<string>>();
  private readonly closeListeners = new Set<Listener<{ code: number }>>();

  constructor(opts: WsClientOptions = {}) {
    this.mintTicket = opts.mintTicket ?? defaultMintTicket;
    this.socketUrl = opts.socketUrl ?? defaultSocketUrl;
    this.factory = opts.factory === undefined ? defaultFactory : opts.factory;
    this.onStatus = opts.onStatus ?? (() => undefined);
    this.onUnauthorized = opts.onUnauthorized ?? (() => undefined);
    this.onMinClientGeneration =
      opts.onMinClientGeneration ?? ((min) => void onMinClientGeneration(min));
    this.log = opts.log ?? (() => undefined);
    this.heartbeatMs = opts.heartbeatMs ?? DEFAULT_HEARTBEAT_MS;
    this.pongTimeoutMs = opts.pongTimeoutMs ?? DEFAULT_PONG_TIMEOUT_MS;
    this.downAfterMs = opts.downAfterMs ?? DEFAULT_DOWN_AFTER_MS;
    this.stableAfterMs = opts.stableAfterMs ?? DEFAULT_STABLE_AFTER_MS;
    this.backoff = {
      floorMs: opts.backoff?.floorMs ?? DEFAULT_SSE_BACKOFF.floorMs,
      capMs: opts.backoff?.capMs ?? DEFAULT_SSE_BACKOFF.capMs,
      jitter: opts.backoff?.jitter ?? Math.random,
    };
    this.timers = opts.timers ?? DEFAULT_TIMERS;
  }

  get status(): RealtimeStatus {
    return this._status;
  }

  /** The id the server minted for this socket in its `welcome`; null until then. */
  get peerId(): string | null {
    return this._peerId;
  }

  /** The inbound budget the server named in its last `welcome`; null from a server
   *  that predates it, or before the first one. */
  get limits(): SocketLimits | null {
    return this._limits;
  }

  /** The channels callers currently hold, in subscription order. */
  get channels(): string[] {
    return [...this.refs.keys()];
  }

  /** Idempotent. Without a WebSocket in the environment the client reports `down` and stays idle. */
  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.attempt = 0;
    this.remintedOnce = false;
    this.refreshedOnce = false;
    if (this.factory === null) {
      this.stopped = true;
      this.setStatus("down");
      return;
    }
    this.setStatus("connecting");
    void this.connect();
  }

  /** Close the socket and cancel every timer; the status returns to `idle`. */
  stop(): void {
    this.stopped = true;
    this.teardown(1000, "client stopped");
    this.setStatus("idle");
  }

  /** Hold a channel: it is subscribed now if the socket is open, and again after every
   *  reconnect, until the returned release is called. Ref-counted across callers. */
  subscribe(channel: string): () => void {
    const count = this.refs.get(channel) ?? 0;
    this.refs.set(channel, count + 1);
    if (count === 0) this.send({ t: "subscribe", channel });
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const now = this.refs.get(channel) ?? 0;
      if (now <= 1) {
        this.refs.delete(channel);
        this.send({ t: "unsubscribe", channel });
      } else {
        this.refs.set(channel, now - 1);
      }
    };
  }

  /** Write a frame; true only when it went onto an open, welcomed socket. */
  send(frame: ClientFrame): boolean {
    if (this.socket === null || this.socket.readyState !== OPEN || this._peerId === null) return false;
    try {
      this.socket.send(JSON.stringify(frame));
      return true;
    } catch {
      return false;
    }
  }

  onFrame(listener: Listener<ServerFrame>): () => void {
    this.frameListeners.add(listener);
    return () => void this.frameListeners.delete(listener);
  }

  /** Fires after each `welcome` once the held channels have been re-subscribed. */
  onOpen(listener: Listener<string>): () => void {
    this.openListeners.add(listener);
    return () => void this.openListeners.delete(listener);
  }

  onClose(listener: Listener<{ code: number }>): () => void {
    this.closeListeners.add(listener);
    return () => void this.closeListeners.delete(listener);
  }

  // -- lifecycle -----------------------------------------------------------

  private setStatus(status: RealtimeStatus): void {
    if (this._status === status) return;
    this._status = status;
    this.onStatus(status);
  }

  private clearTimer(
    name: "reconnectTimer" | "downTimer" | "heartbeatTimer" | "pongTimer" | "stableTimer",
  ): void {
    const handle = this[name];
    if (handle !== null) {
      this.timers.clearTimeout(handle);
      this[name] = null;
    }
  }

  private teardown(code: number, reason: string): void {
    this.generation += 1;
    this.clearTimer("reconnectTimer");
    this.clearTimer("downTimer");
    this.clearTimer("stableTimer");
    this.stopHeartbeat();
    const socket = this.socket;
    this.socket = null;
    this._peerId = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      try {
        socket.close(code, reason);
      } catch {
        // already closed
      }
    }
  }

  private async connect(): Promise<void> {
    if (this.stopped || this.factory === null) return;
    const generation = ++this.generation;
    let ticket: WsTicket;
    try {
      ticket = await this.mintTicket();
    } catch (error) {
      if (generation !== this.generation || this.stopped) return;
      if (error instanceof ApiError && error.status === 401) {
        this.unauthorized();
        return;
      }
      this.failed();
      return;
    }
    if (generation !== this.generation || this.stopped) return;
    let socket: WebSocketLike;
    try {
      socket = this.factory(this.socketUrl(ticket.path), [
        WS_SUBPROTOCOL,
        WS_TICKET_SUBPROTOCOL_PREFIX + ticket.ticket,
      ]);
    } catch {
      this.failed();
      return;
    }
    this.socket = socket;
    socket.onopen = () => undefined; // the `welcome` frame, not the open event, is "connected"
    socket.onerror = () => undefined; // a close always follows
    socket.onmessage = (event) => {
      if (generation !== this.generation) return;
      this.receive(parseServerFrame(event.data));
    };
    socket.onclose = (event) => {
      if (generation !== this.generation) return;
      this.closed(event.code);
    };
  }

  private receive(frame: ServerFrame | null): void {
    if (frame === null) return;
    if (frame.t === "welcome") {
      this._peerId = frame.peer_id;
      this._limits = frame.limits;
      this.onMinClientGeneration(frame.min_client_generation);
      this.clearTimer("downTimer");
      this.armStable();
      this.setStatus("connected");
      for (const channel of this.refs.keys()) this.send({ t: "subscribe", channel });
      this.startHeartbeat();
      for (const listener of this.openListeners) listener(frame.peer_id);
      return;
    }
    if (frame.t === "pong") {
      this.clearTimer("pongTimer");
      return;
    }
    for (const listener of this.frameListeners) listener(frame);
  }

  private closed(code: number): void {
    this.stopHeartbeat();
    this.clearTimer("stableTimer");
    this.socket = null;
    this._peerId = null;
    this.generation += 1;
    if (this.stopped) return;
    for (const listener of this.closeListeners) listener({ code });
    switch (code) {
      case CLOSE_CODES.UNAUTHORIZED:
        if (!this.remintedOnce) {
          this.remintedOnce = true;
          this.reconnectNow();
        } else {
          this.failed();
        }
        return;
      case CLOSE_CODES.ORIGIN_FORBIDDEN:
        this.log("realtime socket refused for this origin; not retrying", { code });
        this.stopped = true;
        this.clearTimer("downTimer");
        this.setStatus("down");
        return;
      case CLOSE_CODES.SESSION_EXPIRED:
        // A fresh ticket is the right answer to an expired session, and it is worth one
        // immediate try. It is only ever worth one: a mint that keeps succeeding against a
        // server that keeps closing 4408 — skew across replicas, a ticket already past its
        // life when it is issued — is an unthrottled mint-and-handshake loop otherwise.
        if (!this.refreshedOnce) {
          this.refreshedOnce = true;
          this.reconnectNow();
        } else {
          this.failed();
        }
        return;
      case CLOSE_CODES.FRAME_TOO_LARGE:
        this.log("realtime socket closed on an oversized frame", { code });
        this.failed();
        return;
      case CLOSE_CODES.UNAVAILABLE:
        // The replica that answered runs without the outbox listener, so it could only
        // ever show us what it wrote itself. Backing off and re-minting is what lands the
        // next socket on one that can deliver; reconnecting at once would spin, because
        // refusing at the handshake costs the replica nothing. Same path as the default,
        // named because it is a routing outcome and not a failure of this client.
        this.failed();
        return;
      case CLOSE_PONG_TIMEOUT:
        // A dead connection, not a refusal. It still backs off from wherever the counter
        // stands: a socket that lived long enough to miss a keepalive has already been
        // welcomed for longer than the stability window, so the counter is at the floor
        // anyway — and one that has not is precisely the flapping case.
        this.failed();
        return;
      default:
        this.failed();
    }
  }

  private reconnectNow(): void {
    if (this._status !== "down") this.setStatus("reconnecting");
    this.armDown();
    void this.connect();
  }

  /** Start counting the connection as stable. Only when it lasts does the reconnect delay —
   *  and the one immediate re-mint a 4401 or a 4408 is allowed — return to where a first connection
   *  starts. A socket dropped before then keeps backing off. */
  private armStable(): void {
    this.clearTimer("stableTimer");
    this.stableTimer = this.timers.setTimeout(() => {
      this.stableTimer = null;
      this.attempt = 0;
      this.remintedOnce = false;
      this.refreshedOnce = false;
    }, this.stableAfterMs);
  }

  private armDown(): void {
    if (this.downTimer !== null || this._status === "down") return;
    this.downTimer = this.timers.setTimeout(() => {
      this.downTimer = null;
      if (!this.stopped) this.setStatus("down");
    }, this.downAfterMs);
  }

  private failed(): void {
    if (this.stopped) return;
    if (this._status !== "down") this.setStatus("reconnecting");
    this.armDown();
    const delay = reconnectDelayMs(this.attempt, this.backoff, this.backoff.jitter());
    this.attempt += 1;
    this.clearTimer("reconnectTimer");
    this.reconnectTimer = this.timers.setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }

  private unauthorized(): void {
    this.stopped = true;
    this.teardown(1000, "unauthorized");
    this.setStatus("idle");
    this.onUnauthorized();
  }

  // -- liveness -------------------------------------------------------------

  private startHeartbeat(): void {
    this.stopHeartbeat();
    const tick = (): void => {
      this.heartbeatTimer = null;
      if (!this.send({ t: "ping" })) return;
      if (this.pongTimer === null) {
        this.pongTimer = this.timers.setTimeout(() => {
          this.pongTimer = null;
          const socket = this.socket;
          if (socket) {
            // Drive our own close path: the fake in a test and a real socket both report it.
            try {
              socket.close(CLOSE_PONG_TIMEOUT, "pong timeout");
            } catch {
              // already closing
            }
            this.closed(CLOSE_PONG_TIMEOUT);
          }
        }, this.pongTimeoutMs);
      }
      this.heartbeatTimer = this.timers.setTimeout(tick, this.heartbeatMs);
    };
    this.heartbeatTimer = this.timers.setTimeout(tick, this.heartbeatMs);
  }

  private stopHeartbeat(): void {
    this.clearTimer("heartbeatTimer");
    this.clearTimer("pongTimer");
  }
}

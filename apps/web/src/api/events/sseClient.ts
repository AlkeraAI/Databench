// A server-sent-events client over `fetch`, not `EventSource`.
//
// The browser's EventSource cannot set a request header, retries on its own
// fixed schedule, hides the HTTP status of a refused connection behind a bare
// error event, and never gives up. This client owns every one of those
// decisions: the session cookie rides the request (`credentials: "include"`),
// the resume cursor travels BOTH as the standard `Last-Event-ID` header and as
// `?after=<id>` (the server prefers the header; the query survives a proxy that
// strips unknown headers), a refused connection is classified by its status —
// 401 goes to the refresh below, anything else backs off — and reconnects follow
// an exponential schedule with a two-second floor and a one-minute cap so a
// fleet-wide restart never lands every client back in the same instant.
//
// The stream is the one authenticated transport that does not ride the `api` client, so it
// carries its own copy of the session rule every other request gets: a refusal is answered
// with ONE silent refresh and a single retry, and only a refusal that survives that hands
// the user to login. Without it the stream is the first thing to notice a lapsed access
// cookie — it reconnects on its own deadline, long after the last click — and a session the
// app could have renewed would end at the reconnect instead.
//
// The wire format (see packages/api-core/alkera_core/schemas/realtime/README.md):
// `id:` / `event:` / `data:` blocks ended by a blank line; `:` comments are the
// keepalive; `retry:` is the server's hint; `event: error` precedes a close the
// client must not retry.

import {
  SSE_BACKOFF_CAP_MS,
  SSE_BACKOFF_FLOOR_MS,
  SSE_DOWN_AFTER_MS,
  SSE_STALL_MS,
} from "@/lib/limits";
import { parseRetryAfter } from "@/lib/retryAfter";

export type SseStatus = "idle" | "connecting" | "connected" | "reconnecting" | "down";

export interface SseFrame {
  /** The `id:` this frame carried, or null when it carried none (a `reset`). */
  id: string | null;
  type: string;
  data: string;
}

/** `fetch`'s shape as this client calls it: a built `Request` (the way every other call in
 *  the app reaches the network, so a test helper reading the first argument as a Request
 *  keeps working) plus an init carrying only the abort signal. */
export type FetchLike = (input: Request, init?: RequestInit) => Promise<Response>;

export interface SseBackoff {
  floorMs: number;
  capMs: number;
  /** A number in [0, 1); Math.random in production, pinned in a test. */
  jitter: () => number;
}

export interface SseTimers {
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
}

export interface SseClientOptions {
  /** `${apiBaseUrl}/api/v1/events`. */
  url: string;
  onFrame: (frame: SseFrame) => void;
  onStatus: (status: SseStatus) => void;
  /** The stream was refused with 401, or the server ended it with `error{unauthorized}`, and
   *  a silent refresh did not recover it. */
  onUnauthorized: () => void;
  /** Exchange the refresh cookie for a fresh access token. Resolves true when the session was
   *  renewed, in which case the stream is retried once before giving up. Defaults to "there is
   *  nothing to try", which keeps the client usable on its own. */
  refresh?: () => Promise<boolean>;
  fetch?: FetchLike;
  /** Decorates whichever fetch the client ends up with (the one given, or the global):
   *  the portal names the tab's org on the stream this way. */
  wrapFetch?: (fetch: FetchLike) => FetchLike;
  backoff?: Partial<SseBackoff>;
  /** How long `reconnecting` may last before the client reports `down` (default 5 s). */
  downAfterMs?: number;
  /** Silence on an open stream longer than this is a dead connection (default 45 s: three
   *  missed server keepalives). */
  stallMs?: number;
  timers?: SseTimers;
}

export const DEFAULT_SSE_BACKOFF = {
  floorMs: SSE_BACKOFF_FLOOR_MS,
  capMs: SSE_BACKOFF_CAP_MS,
} as const;
export const DEFAULT_DOWN_AFTER_MS = SSE_DOWN_AFTER_MS;
export const DEFAULT_STALL_MS = SSE_STALL_MS;

const DEFAULT_TIMERS: SseTimers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** The delay before reconnect attempt `attempt` (0-based): the floor doubled per attempt up to
 *  the cap, plus a jitter in [0, floor). Never below the floor. */
export function reconnectDelayMs(
  attempt: number,
  backoff: { floorMs: number; capMs: number },
  jitter01: number,
): number {
  const exponential = Math.min(backoff.floorMs * 2 ** Math.max(0, Math.floor(attempt)), backoff.capMs);
  const clamped = Math.min(Math.max(jitter01, 0), 0.999_999);
  return exponential + clamped * backoff.floorMs;
}

/**
 * An incremental `text/event-stream` parser: feed it decoded text in any chunking and it
 * returns the frames that completed. Follows the WHATWG dispatch rules — a frame with no
 * `data:` line is not dispatched, `id:` updates the resume cursor even on such a frame, an id
 * containing U+0000 is ignored, a leading BOM is dropped, and CR / LF / CRLF all end a line.
 */
export class SseParser {
  private buffer = "";
  private first = true;
  private event: string | null = null;
  private data: string[] = [];
  private frameId: string | null = null;
  private _lastEventId: string | null;
  private _retryMs: number | null = null;

  constructor(lastEventId: string | null = null) {
    this._lastEventId = lastEventId;
  }

  /** The most recent `id:` seen — the cursor to resume from. */
  get lastEventId(): string | null {
    return this._lastEventId;
  }

  /** The server's most recent `retry:` hint. */
  get retryMs(): number | null {
    return this._retryMs;
  }

  feed(text: string): SseFrame[] {
    if (this.first) {
      this.first = false;
      if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    }
    this.buffer += text;
    const out: SseFrame[] = [];
    for (;;) {
      const cr = this.buffer.indexOf("\r");
      const lf = this.buffer.indexOf("\n");
      let end: number;
      let skip: number;
      if (cr === -1 && lf === -1) break;
      if (cr !== -1 && (lf === -1 || cr < lf)) {
        // A CR at the very end of the buffer may be the first half of a CRLF; wait for more.
        if (cr === this.buffer.length - 1) break;
        end = cr;
        skip = this.buffer.charAt(cr + 1) === "\n" ? 2 : 1;
      } else {
        end = lf;
        skip = 1;
      }
      const line = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + skip);
      const frame = this.line(line);
      if (frame) out.push(frame);
    }
    return out;
  }

  private line(line: string): SseFrame | null {
    if (line === "") return this.dispatch();
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    switch (field) {
      case "event":
        this.event = value;
        break;
      case "data":
        this.data.push(value);
        break;
      case "id":
        if (!value.includes("\u0000")) {
          this._lastEventId = value;
          this.frameId = value;
        }
        break;
      case "retry":
        if (/^\d+$/.test(value)) this._retryMs = Number(value);
        break;
      default:
        break;
    }
    return null;
  }

  private dispatch(): SseFrame | null {
    const data = this.data;
    const type = this.event ?? "message";
    const id = this.frameId;
    this.event = null;
    this.data = [];
    this.frameId = null;
    if (data.length === 0) return null;
    return { id, type, data: data.join("\n") };
  }
}

export class SseClient {
  private readonly opts: SseClientOptions;
  private readonly fetchImpl: FetchLike | null;
  private readonly refresh: () => Promise<boolean>;
  private readonly backoff: SseBackoff;
  private readonly timers: SseTimers;
  private readonly downAfterMs: number;
  private readonly stallMs: number;

  private _status: SseStatus = "idle";
  private cursor: string | null = null;
  private stopped = true;
  private attempt = 0;
  private generation = 0;
  private controller: AbortController | null = null;
  private reconnectTimer: unknown = null;
  private downTimer: unknown = null;
  private stallTimer: unknown = null;
  /** One refresh per refusal, not one per stream: a connection that came back resets it, so a
   *  session renewed an hour ago is still allowed its attempt when the next refusal arrives.
   *  Without the reset a long-lived tab would get exactly one renewal for its whole life; with
   *  no reset at all a server that answers 401 whatever we do would refresh in a loop. */
  private refreshedOnce = false;

  constructor(opts: SseClientOptions) {
    this.opts = opts;
    this.refresh = opts.refresh ?? (() => Promise.resolve(false));
    const base: FetchLike | null =
      opts.fetch ??
      (typeof globalThis.fetch === "function"
        ? (input, init) => globalThis.fetch(input, init)
        : null);
    this.fetchImpl = base && opts.wrapFetch ? opts.wrapFetch(base) : base;
    this.backoff = {
      floorMs: opts.backoff?.floorMs ?? DEFAULT_SSE_BACKOFF.floorMs,
      capMs: opts.backoff?.capMs ?? DEFAULT_SSE_BACKOFF.capMs,
      jitter: opts.backoff?.jitter ?? Math.random,
    };
    this.timers = opts.timers ?? DEFAULT_TIMERS;
    this.downAfterMs = opts.downAfterMs ?? DEFAULT_DOWN_AFTER_MS;
    this.stallMs = opts.stallMs ?? DEFAULT_STALL_MS;
  }

  get status(): SseStatus {
    return this._status;
  }

  /** The cursor the next connection resumes after. */
  get lastEventId(): string | null {
    return this.cursor;
  }

  /** Idempotent. With no `fetch` in the environment the client reports `down` once and stays
   *  idle rather than throwing. */
  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.attempt = 0;
    this.refreshedOnce = false;
    if (this.fetchImpl === null) {
      this.stopped = true;
      this.setStatus("down");
      return;
    }
    this.setStatus("connecting");
    void this.connect();
  }

  /** The network is back: a stream waiting out its backoff tries again now,
   *  from the first rung, rather than at the end of a wait that grew while the
   *  machine had no network to try with. A stream that is connected, or already
   *  connecting, is left alone. */
  retryNow(): void {
    if (this.stopped || this.fetchImpl === null) return;
    if (this._status === "connected" || this._status === "connecting") return;
    if (this.reconnectTimer === null) return;
    this.clearTimer("reconnectTimer");
    this.attempt = 0;
    void this.connect();
  }

  /** Abort the stream and every timer; the status returns to `idle`. */
  stop(): void {
    this.stopped = true;
    this.teardown();
    this.setStatus("idle");
  }

  private teardown(): void {
    this.generation += 1;
    this.controller?.abort();
    this.controller = null;
    this.clearTimer("reconnectTimer");
    this.clearTimer("downTimer");
    this.clearTimer("stallTimer");
  }

  private clearTimer(name: "reconnectTimer" | "downTimer" | "stallTimer"): void {
    const handle = this[name];
    if (handle !== null) {
      this.timers.clearTimeout(handle);
      this[name] = null;
    }
  }

  private setStatus(status: SseStatus): void {
    if (this._status === status) return;
    this._status = status;
    this.opts.onStatus(status);
  }

  private urlFor(): string {
    if (this.cursor === null) return this.opts.url;
    const joiner = this.opts.url.includes("?") ? "&" : "?";
    return `${this.opts.url}${joiner}after=${encodeURIComponent(this.cursor)}`;
  }

  private async connect(): Promise<void> {
    if (this.stopped || this.fetchImpl === null) return;
    const generation = ++this.generation;
    const controller = new AbortController();
    this.controller = controller;
    const headers: Record<string, string> = {
      Accept: "text/event-stream",
      "Cache-Control": "no-store",
    };
    if (this.cursor !== null) headers["Last-Event-ID"] = this.cursor;

    let response: Response;
    try {
      // The signal rides fetch's init rather than the Request: a Request built with a signal
      // is bound to that signal forever, and this one is aborted per attempt.
      const request = new Request(this.urlFor(), {
        method: "GET",
        headers,
        credentials: "include",
        cache: "no-store",
      });
      response = await this.fetchImpl(request, { signal: controller.signal });
    } catch {
      if (generation !== this.generation || this.stopped) return;
      this.failed();
      return;
    }
    if (generation !== this.generation || this.stopped) return;
    // A stubbed or broken fetch may resolve with something that is not a Response.
    const status = typeof response?.status === "number" ? response.status : 0;
    if (status === 401) {
      await this.refused(generation);
      return;
    }
    if (status < 200 || status >= 300 || !response.body) {
      this.failed(status === 0 ? null : parseRetryAfter(response.headers.get("retry-after")));
      return;
    }

    this.attempt = 0;
    this.refreshedOnce = false;
    this.clearTimer("downTimer");
    this.setStatus("connected");
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    const parser = new SseParser(this.cursor);
    this.armStall(controller);
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (generation !== this.generation || this.stopped) return;
        if (done) break;
        this.armStall(controller);
        for (const frame of parser.feed(decoder.decode(value, { stream: true }))) {
          if (this.handle(frame, parser)) return;
        }
        // An `id:` on a frame with no `data:` dispatches nothing yet still moves the cursor
        // (the server sends one after a straggler to put the cursor back where it was), so
        // the resume cursor follows the parser, not only the frames that were dispatched.
        if (parser.lastEventId !== null) this.cursor = parser.lastEventId;
      }
    } catch {
      if (generation !== this.generation || this.stopped) return;
      this.clearTimer("stallTimer");
      this.failed();
      return;
    }
    this.clearTimer("stallTimer");
    if (generation !== this.generation || this.stopped) return;
    // The server ended the stream on purpose (its deadline): come straight back on the floor
    // schedule rather than escalating as if it had failed.
    this.attempt = 0;
    this.failed();
  }

  /** Returns true when the frame ended the client (nothing more may be read). */
  private handle(frame: SseFrame, parser: SseParser): boolean {
    if (frame.type === "error") {
      let code = "";
      try {
        code = String((JSON.parse(frame.data) as { code?: unknown }).code ?? "");
      } catch {
        code = "";
      }
      if (code === "unauthorized") {
        // The server ended the stream on the session, not on this request: the same refusal
        // an ordinary 401 is, reached from the other side, so it gets the same one refresh.
        void this.refused(this.generation);
      } else {
        // The README: an `error` frame precedes a close the client must not retry.
        this.stopped = true;
        this.teardown();
        this.setStatus("down");
      }
      return true;
    }
    if (frame.id !== null) this.cursor = parser.lastEventId;
    this.opts.onFrame(frame);
    return false;
  }

  private armStall(controller: AbortController): void {
    this.clearTimer("stallTimer");
    this.stallTimer = this.timers.setTimeout(() => {
      this.stallTimer = null;
      controller.abort();
    }, this.stallMs);
  }

  /** A refusal: try ONE silent refresh and come straight back, else hand the user to login.
   *  The retry is immediate rather than backed off — the session was just renewed, so the
   *  next attempt is expected to succeed and the person is mid-work. */
  private async refused(generation: number): Promise<void> {
    if (this.refreshedOnce) {
      this.unauthorized();
      return;
    }
    this.refreshedOnce = true;
    // Drop the refused (or server-ended) connection before asking for a new one, so the retry
    // is not racing a response this client has stopped reading.
    this.controller?.abort();
    this.controller = null;
    let renewed = false;
    try {
      renewed = await this.refresh();
    } catch {
      renewed = false;
    }
    if (generation !== this.generation || this.stopped) return;
    if (!renewed) {
      this.unauthorized();
      return;
    }
    this.attempt = 0;
    this.setStatus("reconnecting");
    void this.connect();
  }

  private unauthorized(): void {
    this.stopped = true;
    this.teardown();
    this.setStatus("idle");
    this.opts.onUnauthorized();
  }

  /** Schedule the next attempt. `minDelayMs` lets a `Retry-After` push the attempt later than
   *  the backoff alone would. */
  private failed(minDelayMs: number | null = null): void {
    if (this.stopped) return;
    this.controller = null;
    if (this._status !== "down") {
      this.setStatus("reconnecting");
      if (this.downTimer === null) {
        this.downTimer = this.timers.setTimeout(() => {
          this.downTimer = null;
          if (!this.stopped) this.setStatus("down");
        }, this.downAfterMs);
      }
    }
    const delay = Math.max(
      reconnectDelayMs(this.attempt, this.backoff, this.backoff.jitter()),
      minDelayMs ?? 0,
    );
    this.attempt += 1;
    this.clearTimer("reconnectTimer");
    this.reconnectTimer = this.timers.setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }
}

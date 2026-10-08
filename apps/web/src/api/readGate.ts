// The pause a refused READ puts under every other read.
//
// The portal's reads are a herd, not a queue: one chat tab holds a dozen polls
// and the event stream re-validates them together, so the limiter refuses them
// as a burst and every one of them is refused at once. React Query's retry
// ladder already re-asks a refused read on the server's terms — but the ladder
// is per query, and `refetchInterval` is not on the ladder at all. Once a poll's
// attempts are spent, its next tick fires regardless of what the server just
// said, which is how five idle tabs produced nineteen hundred 429s in half an
// hour.
//
// So the wait lives one level down, at the fetch itself, where every read
// converges whatever hook or transport issued it: the server refuses one read,
// and every read holds for as long as it asked.
//
// Three things make that safe:
//   - a held read is HELD, not failed. React Query sees a fetch still in flight,
//     keeps the data on screen, and — because it will not start a second fetch
//     for a query already fetching — the interval ticks that land inside the
//     pause fold into the one request waiting at the gate, instead of each
//     becoming a request of its own;
//   - only reads wait. A write is the person's own action and has its own
//     visible refusal; making the user's send queue behind a refused poll would
//     trade a noisy background for a product that feels broken. The server's
//     limiter is per class anyway, so a refused read says nothing about a send;
//   - an abort is honoured while waiting, so a query whose component unmounted
//     rejects at the gate rather than holding a request nobody is waiting for.

import { READ_PAUSE_CAP_MS, READ_PAUSE_DEFAULT_MS, RETRY_BACKOFF_FLOOR_MS } from "@/lib/limits";

import { retryAfterHeaderMs } from "./retry";

/** The methods that wait. A body-carrying method is the person acting. */
const GATED_METHODS: ReadonlySet<string> = new Set(["GET", "HEAD"]);

/**
 * The one read that never waits.
 *
 * `/auth/me` is not a poll — it is the shell asking who is reading, once, and
 * nothing at all renders until it answers. It already has the tightest ladder
 * in the portal, already honours `Retry-After`, and already refuses to sit
 * through a wait longer than the shell will (past that it hands the reader the
 * Retry itself). Holding it would park a blank shell behind a refusal that some
 * background poll collected, and the surface whose whole job is to say "could
 * not load your session" would be the thing held back from saying it.
 *
 * It is one request per boot against a storm measured in hundreds, so the trade
 * is not close. Nothing else belongs here: a read that repeats on a timer is
 * exactly what the gate exists for.
 */
const UNGATED_PATHS: ReadonlySet<string> = new Set(["/api/v1/auth/me"]);

function pathOf(input: RequestInfo | URL): string | null {
  const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  try {
    return new URL(href, "http://localhost").pathname;
  } catch {
    return null;
  }
}

/** When the reads may go out again, as a `Date.now()` stamp. Zero means now. */
let pausedUntil = 0;

/** Milliseconds the reads are still held for, or 0 when they are not. */
export function readPauseRemainingMs(now: number = Date.now()): number {
  return Math.max(0, pausedUntil - now);
}

/**
 * Whether a read issued right now would be held.
 *
 * Read by anything that decides to ASK rather than issuing the request itself —
 * the open-folder poll invalidates a query key, and an invalidation during the
 * pause would queue a refetch whose only effect is to arrive at the gate.
 */
export function readsPaused(now: number = Date.now()): boolean {
  return readPauseRemainingMs(now) > 0;
}

/** Drop the pause. A test seam only: nothing in production calls it, and a
 * sign-out does not reload the tab, so a pause armed before it outlives it. */
export function resetReadGate(): void {
  pausedUntil = 0;
}

/**
 * How long a refusal holds the reads.
 *
 * The server's own `Retry-After` wins when it named one, because a limiter that
 * says "in seven seconds" knows its own window. The floor is the same one the
 * retry ladder uses and is load-bearing for the same reason: a proxy spelling
 * the header `0`, empty, or as a moment this browser's clock has already passed
 * would otherwise license an instant re-send of the burst just refused. The cap
 * is where a header stops being protection and starts being a parked tab, and
 * the default covers a limiter that named nothing at all — a refusal with no
 * wait attached is still a refusal, and asking again on the next tick is what
 * produced the storm.
 */
export function readPauseMs(retryAfter: string | null, now: number = Date.now()): number {
  const asked = retryAfterHeaderMs(retryAfter, now);
  return Math.min(
    Math.max(asked ?? READ_PAUSE_DEFAULT_MS, RETRY_BACKOFF_FLOOR_MS),
    READ_PAUSE_CAP_MS,
  );
}

/** Hold the reads. A longer pause replaces a shorter one; a shorter one never
 *  cuts a pause already running short, so a second refusal can only add time. */
function armPause(retryAfter: string | null, now: number): void {
  pausedUntil = Math.max(pausedUntil, now + readPauseMs(retryAfter, now));
}

function abortReason(signal: AbortSignal): unknown {
  return (
    (signal.reason as unknown) ??
    (typeof DOMException === "function"
      ? new DOMException("The read was aborted.", "AbortError")
      : new Error("The read was aborted."))
  );
}

/** Wait `ms`, or reject the moment the caller's signal aborts. */
function sleep(ms: number, signal: AbortSignal | null): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortReason(signal));
      return;
    }
    let timer: ReturnType<typeof setTimeout> | null = null;
    const done = (): void => {
      if (timer !== null) clearTimeout(timer);
      timer = null;
      signal?.removeEventListener("abort", onAbort);
    };
    const onAbort = (): void => {
      done();
      reject(signal ? abortReason(signal) : new Error("aborted"));
    };
    signal?.addEventListener("abort", onAbort);
    timer = setTimeout(() => {
      done();
      resolve();
    }, ms);
  });
}

/** Sit at the gate until the pause is spent. Re-read each time round: another
 *  read refused while this one waits extends the pause, and a request that left
 *  on the old deadline would be exactly the one the server refused again. */
async function holdAtGate(signal: AbortSignal | null): Promise<void> {
  for (;;) {
    const remaining = readPauseRemainingMs();
    if (remaining <= 0) return;
    await sleep(remaining, signal);
  }
}

function methodOf(input: RequestInfo | URL, init?: RequestInit): string {
  const named = init?.method ?? (input instanceof Request ? input.method : undefined);
  return (named ?? "GET").toUpperCase();
}

function signalOf(input: RequestInfo | URL, init?: RequestInit): AbortSignal | null {
  return init?.signal ?? (input instanceof Request ? input.signal : null);
}

/**
 * `fetch`, with the read pause in front of it.
 *
 * Wired in front of every read the portal issues: the typed SDK client every
 * portal query goes through, the cloud-chat transport, and each module that
 * still calls raw `fetch` for a read (the static coverage test in
 * `apps/web/src/tests/api/readGateCoverage.test.ts` names them and fails when one is missed).
 * `globalThis.fetch` is looked up per call, never captured, so a test that swaps
 * the global still sees every request.
 */
export const gatedFetch: typeof fetch = async (input, init) => {
  const path = pathOf(input);
  if (!GATED_METHODS.has(methodOf(input, init)) || (path !== null && UNGATED_PATHS.has(path))) {
    return globalThis.fetch(input, init);
  }

  await holdAtGate(signalOf(input, init));
  // The deadline this request has ALREADY waited out. Only that same deadline
  // may be cleared on success: a refusal that landed while this request was in
  // flight armed a pause this request never waited for, and clearing it would
  // send the rest of the herd straight back into the limiter.
  const waited = pausedUntil;
  const response = await globalThis.fetch(input, init);
  const now = Date.now();
  if (response.status === 429) armPause(response.headers.get("retry-after"), now);
  else if (response.ok && pausedUntil === waited) pausedUntil = 0;
  return response;
};

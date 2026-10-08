// How long a refused read waits before it asks again.
//
// The portal's own reads are what trip the portal's own limiter: one chat page
// holds a dozen queries that the event stream re-validates together, so a
// burst that is refused is refused as a burst. The wait therefore has to do two
// things react-query's default does not — take the server's `Retry-After` as
// the floor when it named one, and spread the retries of every tab that was
// refused in the same second so they do not come back in the same second.

import { ApiError } from "./errors";
import {
  ladderDelay,
  RETRY_AFTER_CAP_MS,
  RETRY_BACKOFF,
  RETRY_BACKOFF_FLOOR_MS,
  RETRY_JITTER_RATIO,
} from "@/lib/limits";

/** The wait the server itself asked for, in milliseconds, or null when it asked
 *  for none. Both wire spellings are read: a delta in seconds and an HTTP date.
 *
 *  The answer can legitimately be zero or negative — an empty header, a literal
 *  `0`, or a date that is already past because this browser's clock runs ahead
 *  of the server's, which is the ordinary case rather than an exotic one. That
 *  is reported honestly here and floored by the caller, so "the server named a
 *  wait" and "the wait is long enough to be worth taking" stay separate. */
export function retryAfterMs(error: unknown, now: number = Date.now()): number | null {
  if (!(error instanceof ApiError)) return null;
  return retryAfterHeaderMs(error.retryAfter, now);
}

/** The same reading, from the header itself rather than from an error built
 *  around it — the read gate holds a `Response` and never mints an `ApiError`
 *  for a refusal nothing throws. One parser, so the pause and the retry ladder
 *  can never disagree about what the server asked for. */
export function retryAfterHeaderMs(header: string | null, now: number = Date.now()): number | null {
  if (header === null) return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds)) {
    return Math.min(Math.max(seconds, 0) * 1_000, RETRY_AFTER_CAP_MS);
  }
  const at = Date.parse(header);
  if (Number.isNaN(at)) return null;
  return Math.min(Math.max(at - now, 0), RETRY_AFTER_CAP_MS);
}

/**
 * The portal-wide `retryDelay`. `attempt` is react-query's zero-based failure
 * count; `random` is injected so a test can pin both ends of the jitter.
 *
 * The server's own wait wins when it named one — a limiter that says "in two
 * seconds" knows something the client does not — and the jitter is added on top
 * so honouring it does not itself become a synchronised stampede.
 *
 * `RETRY_BACKOFF_FLOOR_MS` is the floor under BOTH paths, and the header does
 * not get to go under it. An empty `Retry-After`, a literal `0`, a negative one
 * and a date this browser's clock has already passed all mean "as soon as you
 * like" — and taken at their word they re-send, instantly and three times over,
 * the very burst that was just refused. A server that wants to be asked again
 * sooner than a second does not exist; a proxy that spells the header badly
 * does.
 */
export function queryRetryDelay(
  attempt: number,
  error: unknown,
  random: () => number = Math.random,
): number {
  const asked = retryAfterMs(error);
  const climb = ladderDelay(attempt, RETRY_BACKOFF);
  const base = asked === null ? climb : Math.max(asked, RETRY_BACKOFF_FLOOR_MS);
  return Math.round(base * (1 + RETRY_JITTER_RATIO * random()));
}

/**
 * The same wait, in whole seconds, as a failed-load plate reads it out to the reader.
 *
 * It goes through `retryAfterMs` rather than reading the header itself, so the sentence on screen
 * can never quote a wait the ladder is not actually taking — including the cap, past which the
 * header is treated as a mistake. Rounded up: a plate that says "1 second" for 1.4 s reads as
 * wrong to the person who then counts.
 */
export function retryAfterSeconds(error: unknown): number | undefined {
  const asked = retryAfterMs(error);
  return asked === null ? undefined : Math.ceil(asked / 1_000);
}

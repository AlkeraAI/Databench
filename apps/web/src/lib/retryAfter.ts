// The wait a `Retry-After` header asks for, read one way for every transport
// that honours it on its own (the event stream's reconnect, the upload's part
// retries).

/**
 * The milliseconds a `Retry-After` value asks for, or null when it asks for
 * nothing readable.
 *
 * Both wire spellings: a delta in seconds (what the API's own limiter and body
 * guard send) and an HTTP date (what a proxy in front of it may rewrite it to).
 * A negative delta or a date already past means "now" and reads as 0; an absent,
 * empty or unreadable value reads as null. No cap and no floor: each caller
 * decides how long a wait it will take.
 */
export function parseRetryAfter(header: string | null, now: number = Date.now()): number | null {
  if (header === null) return null;
  const text = header.trim();
  if (text === "") return null;
  const seconds = Number(text);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
  const at = Date.parse(text);
  return Number.isNaN(at) ? null : Math.max(0, at - now);
}

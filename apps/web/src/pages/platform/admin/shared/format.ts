// The admin registry's shared readings. Money on the wire is `*_nanos` (USD ×
// 1e9) and prices are `per_token_nanos`; every page converts ONCE through here so
// a column of dollars reconciles by eye and a raw integer never reaches the
// surface. Dates render in the viewer's locale; counts get a thousands separator.

import { formatUsdNanos } from "@alkera/chat-model";

import { formatDate, formatDateTime, formatDateTimeExact } from "@/lib/format/date";

/** A nanos amount as a ledger reading, e.g. 1_500_000_000 → "$1.50": the product's
 *  money rule, the same one an org admin reads. A rate or a price that needs its
 *  fractions of a cent reads through `./precise` instead. */
export function usd(nanos: number | null | undefined): string {
  if (nanos == null) return "—";
  return formatUsdNanos(nanos);
}

/** A whole count with a thousands separator. */
export function count(n: number): string {
  return n.toLocaleString();
}

/** A date (no time), in the shared portal policy. Empty input reads as "—". */
export function date(iso: string | null | undefined): string {
  return formatDate(iso) || "—";
}

/** A date AND time, in the shared portal policy. Empty input reads as "—". */
export function dateTime(iso: string | null | undefined): string {
  return formatDateTime(iso) || "—";
}

/** The seconds-bearing reading for forensic surfaces (an audit drawer correlated
 *  against server logs). Empty input reads as "—". */
export function dateTimeExact(iso: string | null | undefined): string {
  return formatDateTimeExact(iso) || "—";
}

/** The leading 8 characters of a uuid — enough to recognise a row, short enough
 *  for a column. Pairs with a copy affordance where the full id matters. */
export function shortId(id: string): string {
  return id.slice(0, 8);
}

/** A window key ("30d" / "7d" / "24h") as a human label for a chart caption. */
export function windowLabel(window: string): string {
  const map: Record<string, string> = { "24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days", "90d": "Last 90 days" };
  return map[window] ?? window;
}

/** How an org reads where it must be named in a picker. An org still waiting on its name reads as
 *  unnamed with enough of its id to tell two apart — never an empty option. */
export function orgLabel(org: { id: string; name: string }): string {
  return org.name.trim() ? org.name : `Unnamed org (${org.id.slice(0, 8)})`;
}

const ENTITY_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Whether a route segment could be an org's, a user's or a plan's id at all. A detail route
 *  answers anything else with the not-found page and asks the server nothing: every read the page
 *  makes is keyed by that id, and the server could only refuse it. */
export function isEntityId(segment: string | undefined): segment is string {
  return segment !== undefined && ENTITY_ID.test(segment);
}

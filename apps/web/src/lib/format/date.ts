/* One date policy for every portal surface: the VIEWER'S timezone and locale.
 * The backend records the instant; which calendar day it falls on is the
 * reader's question, and two screens answering it in different timezones once
 * showed one timestamp as two different days. Styles are fixed (medium date,
 * short time) so screens differ only by the reader's own locale, never by
 * which page they happen to be on. */

const DATE = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const DATE_TIME = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const DATE_TIME_EXACT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
});

/* A bare calendar date ("2026-07-28") names a DAY, not an instant. Date.parse
 * reads it as UTC midnight, which the viewer-zone formatters west of UTC shift
 * to the prior evening — so a date-only value pins to UTC and the named day
 * survives every timezone. */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const DATE_UTC = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" });
const DATE_TIME_UTC = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "UTC",
});
const DATE_TIME_EXACT_UTC = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
  timeZone: "UTC",
});

function render(
  iso: string | null | undefined,
  instant: Intl.DateTimeFormat,
  dateOnly: Intl.DateTimeFormat,
): string {
  const t = iso ? Date.parse(iso) : Number.NaN;
  if (Number.isNaN(t)) return "";
  return (iso && DATE_ONLY.test(iso) ? dateOnly : instant).format(t);
}

/** "Jul 24, 2026" in the viewer's locale and timezone; "" for a missing or unparsable instant. */
export function formatDate(iso: string | null | undefined): string {
  return render(iso, DATE, DATE_UTC);
}

/** The calendar day a date field names when the field is stored as UTC midnight
 *  of that day (a recurring grant's end date is sent as `${day}T00:00:00Z`).
 *  The viewer-zone reading would show the day before anywhere west of UTC, so
 *  the admin who typed Sep 29 would read Sep 28 back. Only for such fields —
 *  every other instant goes through {@link formatDate}. */
export function formatCalendarDay(iso: string | null | undefined): string {
  const t = iso ? Date.parse(iso) : Number.NaN;
  return Number.isNaN(t) ? "" : DATE_UTC.format(t);
}

/** "Jul 24, 2026, 9:07 PM" in the viewer's locale and timezone; "" when missing/unparsable. */
export function formatDateTime(iso: string | null | undefined): string {
  return render(iso, DATE_TIME, DATE_TIME_UTC);
}

/** "Jul 24, 2026, 9:07:43 PM" — the seconds-bearing variant for forensic surfaces
 *  (audit rows correlated against server logs). Same zone and locale policy. */
export function formatDateTimeExact(iso: string | null | undefined): string {
  return render(iso, DATE_TIME_EXACT, DATE_TIME_EXACT_UTC);
}

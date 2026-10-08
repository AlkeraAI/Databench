// The coarse relative-time reading ("2h ago") the portal's ledgers share, beside
// the absolute-date policy in date.ts. One ladder, two entry points: the daemon
// speaks epoch ms, the cloud wire speaks ISO.

function ladder(ms: number, now: number): string {
  const sec = Math.max(0, Math.round((now - ms) / 1000));
  if (sec < 60) return "Just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day === 1) return "Yesterday";
  if (day < 7) return `${day}d ago`;
  const wk = Math.round(day / 7);
  return `${wk}w ago`;
}

/** A coarse relative label from a ms timestamp ("2h ago"). The daemon sends 0 for "never",
 *  so a non-positive stamp reads as "—". `now` is injectable to keep preview and tests
 *  deterministic. */
export function relativeTime(ms: number | null, now: number): string {
  if (ms == null || Number.isNaN(ms) || ms <= 0) return "—";
  return ladder(ms, now);
}

/** The same ladder from an ISO timestamp (a `KbItemRead.updated_at`). Only an unparseable
 *  stamp reads as "—"; the wire never sends a sentinel here. */
export function relativeTimeIso(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  return Number.isNaN(then) ? "—" : ladder(then, now);
}

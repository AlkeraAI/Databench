/** A duration as a person reads it: "850 ms", "1.2 s", "2 min 5 s",
 *  "3 h 12 min". The two largest units, the smaller dropped when zero. */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "0 ms";
  if (Math.round(ms) < 1000) return `${Math.round(ms)} ms`;
  const tenths = Math.round(ms / 100) / 10;
  if (tenths < 60) return `${Number.isInteger(tenths) ? tenths : tenths.toFixed(1)} s`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return s === 0 ? `${m} min` : `${m} min ${s} s`;
  }
  const minutes = Math.round(ms / 60000);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

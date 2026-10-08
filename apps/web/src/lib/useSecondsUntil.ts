import { useEffect, useMemo, useState } from "react";

/**
 * Whole seconds until `instant` (an ISO timestamp), ticking down once a second
 * and clamped at 0. Null/undefined/unparseable → 0 (nothing to wait for). The
 * remaining time is always re-derived from the wall clock, so a background tab
 * that throttles timers lands on the right value the moment it ticks again.
 */
export function useSecondsUntil(instant: string | null | undefined): number {
  const target = useMemo(() => (instant ? Date.parse(instant) : Number.NaN), [instant]);
  const [remaining, setRemaining] = useState(() => secondsUntil(target));

  useEffect(() => {
    setRemaining(secondsUntil(target));
    if (Number.isNaN(target) || target <= Date.now()) return;
    const timer = setInterval(() => {
      const next = secondsUntil(target);
      setRemaining(next);
      if (next <= 0) clearInterval(timer);
    }, 1_000);
    return () => clearInterval(timer);
  }, [target]);

  return remaining;
}

function secondsUntil(target: number): number {
  if (Number.isNaN(target)) return 0;
  return Math.max(0, Math.ceil((target - Date.now()) / 1_000));
}

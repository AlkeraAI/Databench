// A clock for a starting machine's "about N min": ticks only while something on
// screen is counting down, so an idle page re-renders for nothing.

import { useEffect, useState } from "react";

export const NOW_TICK_MS = 15_000;

export function useNow(active: boolean, tickMs: number = NOW_TICK_MS): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), tickMs);
    return () => window.clearInterval(id);
  }, [active, tickMs]);
  return now;
}

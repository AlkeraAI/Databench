import { useEffect, useState } from "react";

/** The live query result, or `false` where `matchMedia` isn't available (SSR, or a test env that
 *  hasn't stubbed it). Guards on `matchMedia` itself — `window` can exist without it (jsdom). */
function queryMatches(query: string, enabled: boolean): boolean {
  if (!enabled || typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia(query).matches;
}

/**
 * useMediaQuery — subscribe to a CSS media query, SSR-safe and correct on first paint.
 *
 * The initial value reads the live query synchronously (not after an effect), so a query-driven
 * layout paints in its right state instead of flashing the wrong one for a frame. `enabled=false`
 * pins the result to `false` and skips the listener — for a control that only sometimes reacts to a
 * breakpoint.
 */
export function useMediaQuery(query: string, enabled = true): boolean {
  const [matches, setMatches] = useState(() => queryMatches(query, enabled));
  useEffect(() => {
    if (!enabled || typeof window === "undefined" || typeof window.matchMedia !== "function") {
      setMatches(false);
      return;
    }
    const mq = window.matchMedia(query);
    const apply = () => setMatches(mq.matches);
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [query, enabled]);
  return matches;
}

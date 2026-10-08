import { useEffect, useState } from "react";

/** The one keystroke-to-settled lag every search field in the product waits out. Long enough that
 *  a burst of typing costs one read, short enough that a pause reads as answered. */
export const SEARCH_DEBOUNCE_MS = 250;

/**
 * `value` once it has stopped changing for `delayMs`, and whether it has caught up yet.
 *
 * Every search field in the product types into this one hook: the field stays live under the
 * fingers while the work a query costs — an RPC, a cut of the whole catalog — waits for the pause.
 * A surface reads the second value to know its rows are still answering an older query.
 *
 * A value that CLEARS settles at once, so emptying a search field restores its full list with no
 * wait. Only narrowing pays the delay.
 */
export function useDebouncedValue<T>(value: T, delayMs: number): readonly [T, boolean] {
  const [waited, setWaited] = useState(value);
  useEffect(() => {
    if (!value) return;
    const t = window.setTimeout(() => setWaited(value), delayMs);
    return () => window.clearTimeout(t);
  }, [value, delayMs]);
  // Resolved in the render rather than behind an effect, so the clear lands in the same frame the
  // field empties. A frame late is long enough for anything latching onto the list -- a frozen
  // order, a scroll target -- to latch the narrower one that is already gone.
  const settled = value ? waited : value;
  return [settled, Object.is(settled, value)];
}

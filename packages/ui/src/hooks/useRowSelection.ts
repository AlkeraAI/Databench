import { useCallback, useMemo, useState } from "react";

/**
 * Row-selection state for a `Table` — the select-all + per-row-checkbox bookkeeping that every
 * selectable table would otherwise hand-roll (a `Set`, a `toggle`, a `toggleAll`, an `allOn`
 * derivation). The hook owns the set; the caller renders the checkboxes and reads the flags.
 *
 * `keys` is the CURRENT set of selectable row keys (already filtered / on the active page's data
 * source) — `allOn` / `someOn` / `toggleAll` reason over exactly these, so filtering the rows
 * narrows "select all" to the visible set with no extra work. Selections for keys that leave `keys`
 * stay in the set but are inert; `clear()` or a fresh `toggleAll` resets them.
 */
export interface RowSelection<K> {
  /** The selected keys (may include keys no longer in `keys` — read `count` for the live tally). */
  selected: Set<K>;
  isSelected: (key: K) => boolean;
  toggle: (key: K) => void;
  /** Select every current key, or clear them all when they're already all selected. */
  toggleAll: () => void;
  clear: () => void;
  /** Every current key is selected (false for an empty `keys`). Drives the header checkbox. */
  allOn: boolean;
  /** Some but not all current keys are selected — the header checkbox's indeterminate state. */
  someOn: boolean;
  /** How many current keys are selected. */
  count: number;
}

export function useRowSelection<K>(keys: readonly K[]): RowSelection<K> {
  const [selected, setSelected] = useState<Set<K>>(() => new Set());

  const isSelected = useCallback((key: K) => selected.has(key), [selected]);

  const toggle = useCallback((key: K) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const count = useMemo(() => keys.reduce((n, k) => (selected.has(k) ? n + 1 : n), 0), [keys, selected]);
  // One "all current keys selected?" predicate, derived from the live count; someOn/toggleAll reuse it.
  const allOn = keys.length > 0 && count === keys.length;
  const someOn = count > 0 && !allOn;

  const toggleAll = useCallback(() => {
    // allOn already reflects the current selection, so toggle straight off it — no second scan.
    setSelected(() => (allOn ? new Set() : new Set(keys)));
  }, [allOn, keys]);

  const clear = useCallback(() => setSelected(new Set()), []);

  return { selected, isSelected, toggle, toggleAll, clear, allOn, someOn, count };
}

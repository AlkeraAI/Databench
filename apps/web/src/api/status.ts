// The status a pill draws, as the server wrote it, and the one thing a client
// owes it: reading again when the server said time alone would change it.

import { useEffect } from "react";
import { useQueryClient, type QueryKey } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

export type StatusFact = components["schemas"]["StatusFact"];

/** Never sooner than this after a read, so a clock that disagrees with the
 *  server's cannot turn the recheck into a loop. */
export const RECHECK_FLOOR_MS = 5_000;

/** The soonest `recheck_at` among `facts`, in epoch milliseconds; `null`
 *  when none of them changes by time alone. */
export function soonestRecheck(facts: readonly (StatusFact | null | undefined)[]): number | null {
  let soonest: number | null = null;
  for (const fact of facts) {
    const at = fact?.recheck_at ? Date.parse(fact.recheck_at) : Number.NaN;
    if (Number.isNaN(at)) continue;
    if (soonest === null || at < soonest) soonest = at;
  }
  return soonest;
}

/** Re-read `queryKey` when the soonest of `facts` says its status would
 *  change with no event to announce it (a wait that becomes "stalled"). The
 *  client knows no bound: the server names the moment. */
export function useStatusRecheck(
  facts: readonly (StatusFact | null | undefined)[],
  queryKey: QueryKey,
): void {
  const queryClient = useQueryClient();
  const at = soonestRecheck(facts);
  // The key is spelled by its owner in `keys.ts` and is stable by value.
  const keyId = JSON.stringify(queryKey);
  useEffect(() => {
    if (at === null) return;
    const timer = setTimeout(
      () => {
        void queryClient.invalidateQueries({ queryKey: JSON.parse(keyId) as QueryKey });
      },
      Math.max(at - Date.now(), RECHECK_FLOOR_MS),
    );
    return () => clearTimeout(timer);
  }, [at, keyId, queryClient]);
}

import {
  MutationCache,
  QueryClient,
  type DefaultOptions,
  type QueryKey,
} from "@tanstack/react-query";

import { ApiError } from "./errors";
import { notGone } from "./gone";
import { queryRetryDelay } from "./retry";
import { QUERY_RETRY_ATTEMPTS, QUERY_STALE_MS } from "@/lib/limits";

/** The statuses that SETTLE a read: the server has answered, and asking again gets the
 *  same answer. A surface renders these at once rather than behind a retry ladder.
 *  A 410 is a 404 the server was explicit about. */
/** The refusal that says the client's read was stale; see the mutation policy. */
const CONFLICT = 409;

const SETTLED_STATUSES: ReadonlySet<number> = new Set([401, 404, 410]);

declare module "@tanstack/react-query" {
  interface Register {
    mutationMeta: {
      /**
       * Cache slots to refresh when this mutation succeeds. A declared list is
       * also refreshed when the server refuses the write with a 409 (the write
       * was built on a read that is no longer true, so those slots are stale).
       * - omitted (the default): invalidate EVERYTHING — fail-safe, a mutation
       *   that declares nothing can never leave stale UI on screen.
       * - an array of key prefixes: narrow the awaited refetch window (use when
       *   invalidate-all is measurably wrong, e.g. the webview's daemon queries).
       * - "none": side-effect-only mutations — redirects, emails, logout.
       */
      invalidates?: ReadonlyArray<QueryKey> | "none";
    };
  }
}

/**
 * The ONE invalidation policy. Every mutation on a client built here refreshes
 * the query cache automatically on success — hooks must NOT hand-wire
 * `invalidateQueries` in `onSuccess` (the sanctioned exception: a genuinely
 * conditional mutation like `useChangePlan`, which opts out via meta and
 * invalidates only on its in-place branch). Declaring nothing invalidates
 * everything: active queries refetch now, inactive ones are marked stale and
 * refetch on next mount, so the failure mode of forgetting is a wasted refetch,
 * never a stale screen.
 *
 * The returned promise is awaited by react-query (cache-level `onSuccess` runs
 * before the hook-level and callsite ones, and before `isPending` flips) — so a
 * spinner, toast, or modal-close can no longer outrun the refetch, and
 * `setQueryData` seeds in hook `onSuccess` land AFTER the refetch (seed wins).
 * That ordering is what makes seeds race-safe; if the await is ever removed,
 * the seeds are, too.
 *
 * Accepted trade-off of awaiting invalidate-all: one failing active query holds
 * every declared-nothing mutation on that page through its retry backoff before
 * the refetch settles (the settled statuses are carved out below; a per-query
 * retry override should settle them too). If that ever bites a surface, the remedy
 * is narrowing meta on its mutations — not un-awaiting the policy.
 *
 * A refused mutation moved nothing, so it refreshes nothing, with one
 * exception: a 409 Conflict on a mutation that names its slots. The server is
 * saying the write was built on a read that is no longer true (the box was
 * taken by another org, the row changed under the form), and a screen left on
 * that read offers the same refused write again forever. The slots the
 * mutation declared in `meta.invalidates` are exactly the reads it was built
 * on, so those are refreshed, awaited like a success, and the callsite's
 * `onError` already sees the fresh cache. A mutation that declares nothing
 * gets no blanket refresh on a refusal: re-reading the whole page on the
 * error path would hold every refusal behind the slowest read on it.
 *
 * Queries keep their failures inline (each surface renders its own error state
 * — see the dashboard's ErrorState), so there is no global error toast here. A
 * short stale time + no refetch-on-focus keeps a dense product surface from
 * re-fetching every tab-in.
 */
export function createQueryClient(queryDefaults?: DefaultOptions["queries"]): QueryClient {
  /** Refresh what a mutation's `meta.invalidates` declares (everything when it
   *  declares nothing, nothing for "none"). */
  const refresh = (invalidates: ReadonlyArray<QueryKey> | "none" | undefined): Promise<unknown> | undefined => {
    // `notGone` is the one carve-out to the invalidate-everything above:
    // a read the server has already answered with 404/410 is not re-asked
    // here, because a page holding one deleted resource would otherwise
    // re-ask for it on every mutation, forever.
    //
    // It is a narrowing of the documented fail-safe, so the two things
    // that DO clear a gone read are deliberate and live in the event
    // scheduler: a frame that names that exact entity, and a stream
    // reset. That is where "you may read this after all" arrives — this
    // repo renders a grantable denial as an opaque 404 — so nothing is
    // stranded by leaving it out of a mutation's blanket refresh.
    if (invalidates === "none") return undefined;
    if (invalidates === undefined) return queryClient.invalidateQueries({ predicate: notGone });
    return Promise.all(
      invalidates.map((queryKey) => queryClient.invalidateQueries({ queryKey, predicate: notGone })),
    );
  };
  const queryClient: QueryClient = new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: QUERY_STALE_MS,
        refetchOnWindowFocus: false,
        // A 401 ("this session is gone") and a 404 ("there is no such thing") are
        // ANSWERS, not flakes: asking again gets the same one. Retrying held every
        // awaited mutation through the full backoff ladder, and left a page whose
        // id the server refuses shimmering under its own shell for the length of
        // it — the not-found state was correct and simply arrived seconds late.
        retry: (failureCount, error) =>
          !(error instanceof ApiError && SETTLED_STATUSES.has(error.status)) &&
          failureCount < QUERY_RETRY_ATTEMPTS,
        // A refusal is re-asked on the SERVER's terms when it named any, and on
        // a jittered ladder otherwise: the reads that trip a rate limiter are a
        // page's own burst, so retrying them on a fixed wait re-sends the burst.
        retryDelay: (failureCount, error) => queryRetryDelay(failureCount, error),
        ...queryDefaults,
      },
    },
    mutationCache: new MutationCache({
      onError: (error, _variables, _onMutateResult, mutation) => {
        if (!(error instanceof ApiError && error.status === CONFLICT)) return;
        const declared = mutation.meta?.invalidates;
        if (declared === undefined || declared === "none") return;
        // The refusal stands whatever the refresh does: never let it throw.
        try {
          return refresh(declared);
        } catch {
          return;
        }
      },
      onSuccess: (_data, _variables, _onMutateResult, mutation) => {
        // The server write already succeeded — a policy bug must never flip the
        // mutation into its error state. This try/catch covers only SYNCHRONOUS
        // throws (e.g. malformed meta); the returned promise never rejecting
        // rests on react-query itself: refetchQueries swallows per-query fetch
        // errors when `throwOnError` is unset (query-core `promise.catch(noop)`),
        // which the "refetch that REJECTS" policy test pins. Don't pass
        // throwOnError here or swap in fetchQuery/resetQueries believing this
        // catch is the net — it isn't.
        try {
          return refresh(mutation.meta?.invalidates);
        } catch {
          return;
        }
      },
    }),
  });
  return queryClient;
}

/** The portal's shared client — a module singleton so the whole app (the shell's viewer hooks as
 *  well as each page) reads one cache, and so non-React code (the design preview) can seed a query
 *  via `queryClient.setQueryData(...)` before mount to drive an offline surface. */
export const queryClient = createQueryClient();

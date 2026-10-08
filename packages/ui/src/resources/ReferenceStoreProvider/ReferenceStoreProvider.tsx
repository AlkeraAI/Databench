import {
  createContext,
  useContext,
  useLayoutEffect,
  useRef,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import type { BlobFetcher, BlobReference } from "@alkera/chat-model";

/** Host side-effects for blob references, held in a small external store so the
 *  transcript — reference chips, the inline-blob markdown card, and the in-card
 *  attachment band — reads them without prop-drilling through every tool card.
 *  All optional: a surface that can't fetch blobs (the browser stub) provides
 *  none and the chips render inert, which is correct. */
export interface ReferenceActionsState {
  /** Fetch one page of a blob for the hover preview / full view. */
  fetchBlob?: BlobFetcher;
  /** Open a blob's full, paginated content read-only in the editor page. */
  onOpen?: (reference: BlobReference) => void;
  /** Export a blob to a file (CSV / JSON / raw). The host owns the page-walking
   *  assembly + disk write. Omitted ⇒ no download affordance. */
  onExport?: (reference: BlobReference) => void;
}

interface ReferenceStore {
  getState(): ReferenceActionsState;
  setState(next: ReferenceActionsState): void;
  subscribe(listener: () => void): () => void;
}

function createReferenceStore(initial?: ReferenceActionsState): ReferenceStore {
  let state: ReferenceActionsState = { ...initial };
  const listeners = new Set<() => void>();
  return {
    getState: () => state,
    setState: (next) => {
      state = next;
      for (const listener of listeners) listener();
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

const ReferenceStoreContext = createContext<ReferenceStore | null>(null);

// A single empty store backs every consumer rendered outside a Provider — no
// actions, so chips are inert and the inline-blob card shows its "unavailable"
// state, matching a surface that can't fetch blobs.
const FALLBACK_STORE = createReferenceStore();

/** Provide the reference-actions store to a chat subtree. The store is created
 *  once (seeded with the initial actions, so consumers read correct values on
 *  first paint) and kept current via an effect — a changed host callback
 *  (always a fresh arrow) reaches the consumers without remounting the
 *  transcript, and without a during-render store notification. */
export function ReferenceStoreProvider({
  actions,
  children,
}: {
  actions: ReferenceActionsState | undefined;
  children: ReactNode;
}) {
  const storeRef = useRef<ReferenceStore | null>(null);
  if (!storeRef.current) storeRef.current = createReferenceStore(actions);
  useLayoutEffect(() => {
    storeRef.current?.setState(actions ?? {});
  }, [actions]);
  return <ReferenceStoreContext.Provider value={storeRef.current}>{children}</ReferenceStoreContext.Provider>;
}

/** Read a slice of the reference actions. The selector must return a stable
 *  slice (a field off the state, not a fresh object). Outside a Provider every
 *  action is undefined, so a chip rendered in a host without blob support
 *  stays inert. */
export function useReferenceActions<T>(selector: (state: ReferenceActionsState) => T): T {
  const store = useContext(ReferenceStoreContext) ?? FALLBACK_STORE;
  return useSyncExternalStore(
    store.subscribe,
    () => selector(store.getState()),
    () => selector(store.getState()),
  );
}

// The dashboard data SEAM — a module-level Zustand store the page reads through `useDashboardData()`.
// It resolves a DashboardState (loading / error / ready) regardless of where the data came from.
// A WRITER pushes the state in:
//
//   • LiveDashboardProvider — the shipping app. Runs the open reads (identity, connections, chats)
//     and every installed OVERVIEW_SOURCES entry, adapts them into the view-model, and pushes the
//     resolved state into the store.
//   • DashboardDataProvider — seeds a fixed state (the preview's fixtures, or a test).
//
// The page reads only the store, so the source swap is invisible to it. Whichever writer is mounted
// is the active source; a page with neither shows the store's initial loading state.

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";

import { useChats } from "../../../api/chats";
import { useMyConnections } from "../../../api/connections";
import { useIdentityDashboard } from "../../../api/dashboard";
import { OVERVIEW_SOURCES, type OverviewQuery, type OverviewSlice, type OverviewSource } from "../../../app/extensions/portal";
import { mergeOverview, toDashboardData } from "./adapt";
import type { DashboardState } from "./model";
import { refusalSentence } from "../../../api/errors";

const NOOP = (): void => {};

interface DashboardStore extends DashboardState {
  /** A writer pushes the resolved state in. */
  setResolved: (state: DashboardState) => void;
}

const initialData = () => ({ status: "loading" as const, data: null, errorMessage: null, retry: NOOP });

export const useDashboardStore = create<DashboardStore>((set) => ({
  ...initialData(),
  // The store IS a DashboardState (+ this action), so a shallow merge of the resolved state sets
  // exactly its data fields and leaves the action in place.
  setResolved: (next) => set(next),
}));

/** Restore the store to its initial state — call in a test's `beforeEach` for isolation. */
export function resetDashboardStore(): void {
  useDashboardStore.setState(initialData());
}

/** Read the resolved dashboard state. `useShallow` keeps the tuple stable so the page only re-renders
 *  when one of these four fields moves. */
export function useDashboardData(): DashboardState {
  return useDashboardStore(
    useShallow((s) => ({ status: s.status, data: s.data, errorMessage: s.errorMessage, retry: s.retry })),
  );
}

/** Seed a fixed state into the store (the preview's fixtures, or a test). A thin writer — it renders
 *  its children untouched. */
export function DashboardDataProvider({ value, children }: { value: DashboardState; children: ReactNode }) {
  useEffect(() => {
    useDashboardStore.getState().setResolved(value);
  }, [value]);
  return <>{children}</>;
}

/** The live source: fetch the open reads and every installed source, adapt, resolve the state,
 *  and push it into the store. */
export function LiveDashboardProvider({ children }: { children: ReactNode }) {
  // Frozen before the first render and held in state, so every render calls the same source hooks
  // in the same order.
  const [sources] = useState(() => OVERVIEW_SOURCES.items());
  const state = useLiveDashboardState(sources);
  useEffect(() => {
    useDashboardStore.getState().setResolved(state);
  }, [state]);
  return <>{children}</>;
}

/** Calls each source's hook in order. The list never changes after the first render. */
function useSourceSlices(sources: readonly OverviewSource[]): { queries: OverviewQuery[]; slices: (OverviewSlice | null)[] } {
  const queries: OverviewQuery[] = [];
  const slices: (OverviewSlice | null)[] = [];
  for (const source of sources) {
    const { queries: q, slice } = source.useSlice();
    queries.push(...q);
    slices.push(slice);
  }
  return { queries, slices };
}

/** Fetch every read the overview needs and classify them into a DashboardState: any error →
 *  error; any one not-yet-resolved → loading; all resolved → ready. There is no empty branch —
 *  every card reads correctly at zero, and the chats panel's start action is what a brand-new seat
 *  needs to see. */
function useLiveDashboardState(sources: readonly OverviewSource[]): DashboardState {
  const identity = useIdentityDashboard();
  const connections = useMyConnections();
  const chats = useChats();
  const contributed = useSourceSlices(sources);

  const queries: OverviewQuery[] = [identity, connections, chats, ...contributed.queries];
  const retry = () => {
    for (const q of queries) void q.refetch();
  };
  const errors = queries.map((q) => (q.isError ? q.error : null));
  const firstError = errors.find((e) => e !== null);
  const anyError = queries.some((q) => q.isError);

  return useMemo<DashboardState>(() => {
    if (anyError) {
      return { status: "error", data: null, errorMessage: refusalSentence(firstError), retry };
    }
    const slices = contributed.slices;
    if (!identity.data || !connections.data || !chats.data || slices.some((s) => s === null)) {
      return { status: "loading", data: null, errorMessage: null, retry };
    }
    const base = toDashboardData(
      { identity: identity.data, connections: connections.data, chats: chats.data.items ?? [] },
      Date.now(),
    );
    const data = mergeOverview(base, slices.filter((s): s is OverviewSlice => s !== null));
    return { status: "ready", data, errorMessage: null, retry };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `retry` closes over stable refetch fns, and the contributed slices are spread in as deps
  }, [identity.data, connections.data, chats.data, anyError, firstError, ...contributed.slices]);
}

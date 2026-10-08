// The Teams data SEAM — a module-level Zustand store the page reads through `useTeamsData()`. It
// resolves a TeamsState (the team-list status + the selected team's detail status) from the live
// endpoints, adapted into the graph the page renders.
//
// The live source depends on the page's selection (which team's roster to fetch), so the page runs it
// via `useLiveTeamsSync(selectedId)` — which fetches the selected team's roster (the wire already
// carries each person's direct standing AND the admin reaching the team by descent, so one fetch
// powers both listings and the governance plate) and its invitations, then pushes the resolved
// state into the store. The roster endpoint is team-admin gated: its 403 resolves to a first-class
// `forbidden` detail, not an error. The page reads the resolved state back through `useTeamsData()`;
// only the live source feeds this seam (no preview yet).

import { useEffect, useMemo } from "react";
import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";

import { ApiError, refusalSentence } from "../../../../api/errors";
import { useCurrentUser } from "../../../../api/auth";
import { useTeams, useTeamInvitations, useTeamMembers } from "../../../../api/teams";
import { mapTeam, toTeamsGraph } from "./adapt";
import { landingFor } from "./permissions";
import type { DetailStatus, TeamsState } from "./model";

const NOOP = (): void => {};

interface TeamsStore extends TeamsState {
  setResolved: (state: TeamsState) => void;
}

const initialData = () => ({
  status: "loading" as const,
  graph: null,
  detail: { status: "loading" as const, errorMessage: null },
  errorMessage: null,
  retry: NOOP,
});

export const useTeamsStore = create<TeamsStore>((set) => ({
  ...initialData(),
  // The store IS a TeamsState (+ this action), so a shallow merge of the resolved state sets exactly
  // its data fields and leaves the action in place.
  setResolved: (next) => set(next),
}));

/** Restore the store to its initial state — call in a test's `beforeEach` for isolation. */
export function resetTeamsStore(): void {
  useTeamsStore.setState(initialData());
}

/** Read the resolved Teams state. `useShallow` keeps the tuple stable so a subscriber only re-renders
 *  when one of these fields moves. */
export function useTeamsData(): TeamsState {
  return useTeamsStore(
    useShallow((s) => ({ status: s.status, graph: s.graph, detail: s.detail, errorMessage: s.errorMessage, retry: s.retry })),
  );
}

/** Run the live source for `selectedId` and push its resolved state into the store. The page calls
 *  this (the source depends on the page's selection) and reads the result via `useTeamsData()`. */
export function useLiveTeamsSync(selectedId: string | undefined): void {
  const state = useLiveTeamsState(selectedId);
  useEffect(() => {
    useTeamsStore.getState().setResolved(state);
  }, [state]);
}

const detailFromError = (err: unknown): DetailStatus => (err instanceof ApiError && err.status === 403 ? "forbidden" : "error");

/** Fetch the selected team's roster (+ its invitations) and classify the result into a TeamsState.
 *  Only the team LIST failing fails the whole page. The roster and invitations are admin-gated, so
 *  they are asked for only on a team the viewer administers (`/auth/me` → `admin_team_ids`); any
 *  other team is a `forbidden` detail without a request, and a roster 403 (a list gone stale) lands
 *  on the same surface. With no team in the URL, the selection is where the viewer's standing
 *  lands them (`landingFor`): the root for an org admin, the team a sub-team admin leads. */
function useLiveTeamsState(selectedId: string | undefined): TeamsState {
  const viewer = useCurrentUser();
  const teamsQuery = useTeams();
  const teams = teamsQuery.data;
  const adminTeamIds = viewer.data?.admin_team_ids;

  const rootId = teams?.find((t) => t.is_root)?.id;
  const landing = useMemo(
    () =>
      teams && rootId && viewer.data
        ? landingFor({ teams: teams.map(mapTeam), rootId, viewerId: viewer.data.id, adminTeamIds: adminTeamIds ?? [] })
        : null,
    [teams, rootId, viewer.data, adminTeamIds],
  );
  const targetId = selectedId ?? (landing?.kind === "team" ? landing.teamId : undefined);
  const manages = Boolean(targetId && adminTeamIds?.includes(targetId));

  const members = useTeamMembers(targetId, false, manages);
  const invites = useTeamInvitations(targetId, manages);

  // A per-render closure over React Query's referentially-stable `refetch` fns, so it's deliberately
  // excluded from the state memo's inputs (keeping `state` stable): the captured `refetch`s stay valid
  // even when the closure is a render behind.
  const retry = () => {
    void teamsQuery.refetch();
    if (manages) {
      void members.refetch();
      void invites.refetch();
    }
  };

  return useMemo<TeamsState>(() => {
    if (teamsQuery.isError) {
      const msg = refusalSentence(teamsQuery.error);
      return { status: "error", graph: null, detail: { status: "error", errorMessage: msg }, errorMessage: msg, retry };
    }
    if (!viewer.data || !teamsQuery.data || !rootId) {
      return { status: "loading", graph: null, detail: { status: "loading", errorMessage: null }, errorMessage: null, retry };
    }

    // The selected roster gates the detail; invitations are best-effort and never block or fail the
    // detail on their own.
    let detail: { status: DetailStatus; errorMessage: string | null };
    if (!targetId || !manages) {
      detail = { status: "forbidden", errorMessage: null };
    } else if (members.isLoading) {
      detail = { status: "loading", errorMessage: null };
    } else if (members.isError) {
      detail = { status: detailFromError(members.error), errorMessage: refusalSentence(members.error) };
    } else {
      detail = { status: "ready", errorMessage: null };
    }

    const graph = toTeamsGraph({
      teams: teamsQuery.data,
      viewerId: viewer.data.id,
      adminTeamIds: viewer.data.admin_team_ids ?? [],
      selectedId: targetId ?? rootId,
      members: manages ? (members.data ?? []) : [],
      invites: manages ? (invites.data ?? []) : [],
    });

    return { status: "ready", graph, detail, errorMessage: null, retry };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- retry is a stable callback; listing it would recompute the memo on every render
  }, [
    viewer.data,
    teamsQuery.data,
    teamsQuery.isError,
    teamsQuery.error,
    rootId,
    targetId,
    manages,
    members.data,
    members.isLoading,
    members.isError,
    members.error,
    invites.data,
  ]);
}

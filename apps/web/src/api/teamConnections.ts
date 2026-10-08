// Team Preconfigured Connections — admin CRUD + the member read view + the
// connector form catalog.
//
// A check is a VERIFICATION RECORD, not a timer: the server creates one, the
// worker moves it through queued → running → settled (or the recovery sweep
// abandons it), and every screen reads its state off the record. The verdict
// arrives over the event stream (`connection.verification_changed`,
// `team_connection.probed`, the server's own name for it); while the stream is
// not delivering, the admin list
// polls whichever rows still carry a verification in flight. The record's own
// poll runs regardless of the stream, because reading it is also what re-arms
// the server's dispatch — see `useVerification`.

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "./client";
import { useRealtimeDown } from "./events/status";
import { keys } from "./keys";

export type TeamConnection = components["schemas"]["TeamConnectionRead"];
export type TeamConnectionUpsert = components["schemas"]["TeamConnectionUpsertRequest"];
export type ConnectorFormDescriptor = components["schemas"]["ConnectorFormDescriptor"];
export type VerificationState = components["schemas"]["VerificationState"];

/** A verification that is still going: the record's own poll runs, and a row
 *  carrying one keeps the list's fallback poll alive. */
export function verificationInFlight(state: VerificationState | null | undefined): boolean {
  return state === "queued" || state === "running";
}

/** The connector catalog (form schemas + team-capable methods) — static per
 *  deploy, so cache it hard. */
export function useConnectionForms(enabled = true) {
  return useQuery({
    queryKey: keys.teamConnections.forms,
    queryFn: () =>
      request(api.GET("/api/v1/plugins/connection-forms"), "Couldn't load the connector catalog."),
    staleTime: 5 * 60_000,
    enabled,
  });
}

/** Admin view of one team's preconfigured connections. While the event stream is not
 *  delivering, polls while any row still carries a verification in flight, so the
 *  verdict lands without a manual refresh. */
export function useTeamConnections(teamId: string | null, enabled = true) {
  const down = useRealtimeDown();
  return useQuery({
    queryKey: keys.teamConnections.team(teamId ?? ""),
    queryFn: () =>
      request(
        api.GET("/api/v1/teams/{team_id}/connections", {
          params: { path: { team_id: teamId ?? "" } },
        }),
        "Couldn't load the team's connections.",
      ),
    enabled: enabled && !!teamId,
    // Guarded rather than defaulted, for the reason in `useMyConnections`: this
    // predicate runs inside render and a throw there is not recoverable.
    refetchInterval: (query) =>
      down &&
      Array.isArray(query.state.data) &&
      query.state.data.some((c) => verificationInFlight(c.verification_state))
        ? 2000
        : false,
  });
}

// Each write below touches one team's list AND the Connections page's one list,
// which shows the same row to whoever can use it. The connector catalog is static
// per deploy and a verification record is its own query, so the refresh is
// narrowed to those two through the shared policy (which awaits it before the
// mutation settles) rather than hand-wired in onSuccess.

export function useUpsertTeamConnection(teamId: string) {
  return useMutation({
    mutationFn: (body: TeamConnectionUpsert) =>
      request(
        api.PUT("/api/v1/teams/{team_id}/connections", {
          params: { path: { team_id: teamId } },
          body,
        }),
        "Couldn't save the connection.",
      ),
    meta: { invalidates: [keys.teamConnections.team(teamId), keys.connections.me] },
  });
}

export function useRotateTeamConnectionSecret(teamId: string) {
  return useMutation({
    mutationFn: ({ connectionId, secret }: { connectionId: string; secret: string }) =>
      request(
        api.POST("/api/v1/teams/{team_id}/connections/{connection_id}/rotate-secret", {
          params: { path: { team_id: teamId, connection_id: connectionId } },
          body: { shared_secret: secret },
        }),
        "Couldn't rotate the credential.",
      ),
    meta: { invalidates: [keys.teamConnections.team(teamId), keys.connections.me] },
  });
}

export function useDeleteTeamConnection(teamId: string) {
  return useMutation({
    mutationFn: (connectionId: string) =>
      request(
        api.DELETE("/api/v1/teams/{team_id}/connections/{connection_id}", {
          params: { path: { team_id: teamId, connection_id: connectionId } },
        }),
        "Couldn't remove the connection.",
      ),
    meta: { invalidates: [keys.teamConnections.team(teamId), keys.connections.me] },
  });
}

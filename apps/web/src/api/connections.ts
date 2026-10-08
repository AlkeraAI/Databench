// The Connections page's data: every connection one person can use — their own
// and their teams' — plus the verbs for the ones they own themselves.
//
// A personal connection is the same shape, the same save pipeline and the same
// verification record as a team's; only the routes differ (`/me/connections`
// versus `/teams/{id}/connections`), because the server decides ownership from
// which door the request came through rather than from a field a client could
// set. The team hooks are re-exported here so a caller that switches owners
// switches modules for nothing.

import { useMutation, useQuery } from "@tanstack/react-query";

import { api, request } from "./client";
import { useRealtimeDown } from "./events/status";
import { keys } from "./keys";
import { verificationInFlight, type TeamConnection, type TeamConnectionUpsert } from "./teamConnections";

export {
  useConnectionForms,
  useDeleteTeamConnection,
  useRotateTeamConnectionSecret,
  useTeamConnections,
  useUpsertTeamConnection,
  verificationInFlight,
} from "./teamConnections";
export type {
  ConnectorFormDescriptor,
  TeamConnection,
  TeamConnectionUpsert,
  VerificationState,
} from "./teamConnections";

/** Every connection the signed-in person can use: their own rows and the rows of
 *  every team they belong to, each carrying `can_manage` — whether THEY may edit,
 *  rotate or remove it. Polls while a row is still being checked and the event
 *  stream is not delivering, exactly as the team list does. */
export function useMyConnections(enabled = true) {
  const down = useRealtimeDown();
  return useQuery({
    queryKey: keys.connections.me,
    queryFn: () => request(api.GET("/api/v1/me/connections"), "Couldn't load your connections."),
    enabled,
    // `Array.isArray` rather than `?? []`: a `refetchInterval` runs inside
    // render, so a body that is not the list this predicate assumes does not
    // merely mis-time the poll — it throws where React cannot recover and takes
    // the surface down with it. The guard is the same one
    // `refetchWhileErroredOrEmpty` already carries.
    refetchInterval: (query) =>
      down &&
      Array.isArray(query.state.data) &&
      query.state.data.some((c) => verificationInFlight(c.verification_state))
        ? 2000
        : false,
  });
}

export function useUpsertMyConnection() {
  return useMutation({
    mutationFn: (body: TeamConnectionUpsert): Promise<TeamConnection> =>
      request(api.PUT("/api/v1/me/connections", { body }), "Couldn't save the connection."),
    meta: { invalidates: [keys.connections.me] },
  });
}

/** Re-address one connection: `teamId` names the team it becomes, `null` makes
 *  it the caller's own. One route for every direction — the row keeps its id,
 *  its configuration and its stored credential, so nothing is retyped.
 *
 *  Both ends of the move change, so this invalidates every connection list
 *  rather than the two it can name: the page shows the team it left beside the
 *  one it joined. */
export function useMoveConnectionOwner() {
  return useMutation({
    mutationFn: ({
      connectionId,
      teamId,
    }: {
      connectionId: string;
      teamId: string | null;
    }): Promise<TeamConnection> =>
      request(
        api.POST("/api/v1/connections/{connection_id}/owner", {
          params: { path: { connection_id: connectionId } },
          body: { team_id: teamId },
        }),
        "Couldn't change who this connection is for.",
      ),
    meta: { invalidates: [keys.teamConnections.all, keys.connections.me] },
  });
}

export function useRotateMyConnectionSecret() {
  return useMutation({
    mutationFn: ({ connectionId, secret }: { connectionId: string; secret: string }) =>
      request(
        api.POST("/api/v1/me/connections/{connection_id}/rotate-secret", {
          params: { path: { connection_id: connectionId } },
          body: { shared_secret: secret },
        }),
        "Couldn't rotate the credential.",
      ),
    meta: { invalidates: [keys.connections.me] },
  });
}

export function useDeleteMyConnection() {
  return useMutation({
    mutationFn: (connectionId: string) =>
      request(
        api.DELETE("/api/v1/me/connections/{connection_id}", {
          params: { path: { connection_id: connectionId } },
        }),
        "Couldn't remove the connection.",
      ),
    meta: { invalidates: [keys.connections.me] },
  });
}

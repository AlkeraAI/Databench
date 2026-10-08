// React Query hooks for the org's team graph — the real backend behind the
// Teams page. Three resources, one module (they move together): teams,
// memberships, invitations. Every shape is the SDK's generated type, so the
// page never hand-types a response.
//
// The roster endpoint (`/teams/{id}/members`) is team-admin gated: a non-admin
// gets 403. That 403 is an EXPECTED answer ("you don't manage this team"), not a
// transient failure — the seam renders a first-class forbidden surface for it, so
// the query must surface the status (via ApiError) and must NOT retry it.
//
// A person stands on a team two ways, and the wire states both per row: the
// membership row written on the team (`direct_role`) and the admin reaching it from
// a team above (`descent_role` + `descent_from_team_*`), with `role` the standing
// the two make. Nothing here recomputes descent; these hooks just fetch.

import { useMutation, useQueries, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { ApiError } from "./errors";
import { api, request } from "./client";
import { keys } from "./keys";

export type Team = components["schemas"]["TeamRead"];
export type TeamCreate = components["schemas"]["TeamCreate"];
export type TeamMember = components["schemas"]["TeamMemberRead"];
export type TeamRole = components["schemas"]["TeamRole"];
export type Invitation = components["schemas"]["InvitationRead"];
export type InvitationCreate = components["schemas"]["InvitationCreate"];

/** A 401 (session gone), a 403 (not a team admin), and a 404 (team gone) are real
 *  answers, not flakes — don't burn retries on them; everything else gets one retry.
 *  Settling the 401 here matters beyond politeness: this override REPLACES the
 *  client-wide 401-aware retry default, and a retried 401 would hold every awaited
 *  mutation on the page through its backoff (see api/queryClient.ts). */
export const settleStatusError = (count: number, err: unknown): boolean =>
  !(err instanceof ApiError && (err.status === 401 || err.status === 403 || err.status === 404)) && count < 1;

// ---- queries --------------------------------------------------------------

/** Every team in the caller's org, each carrying its materialized `member_count`. */
export function useTeams(enabled = true) {
  return useQuery({
    queryKey: keys.teams.all,
    queryFn: () => request(api.GET("/api/v1/teams"), "could not load your teams"),
    enabled,
  });
}

/**
 * The enriched roster (names + emails + per-team role) for one team. Team-admin
 * gated — a 403 throws an {@link ApiError} the seam maps to its forbidden state.
 * `includeDescendants` returns one row per (user, team) across the whole subtree,
 * which is what lets the adapter tell a direct member from an inherited one.
 */
export function useTeamMembers(teamId: string | undefined, includeDescendants: boolean, enabled = true) {
  return useQuery({
    queryKey: keys.teams.members(teamId, includeDescendants),
    enabled: Boolean(teamId) && enabled,
    retry: settleStatusError,
    queryFn: () =>
      request(
        api.GET("/api/v1/teams/{team_id}/members", {
          params: { path: { team_id: teamId! }, query: { include_descendants: includeDescendants } },
        }),
        "could not load this team's roster",
      ),
  });
}

/**
 * The rosters of several teams at once, each with its sub-teams, under the same
 * cache slots {@link useTeamMembers} reads: who a team admin may name across
 * every team they administer. A team the reader may not read drops out rather
 * than failing the rest.
 */
export function useTeamRosters(teamIds: readonly string[], enabled = true) {
  return useQueries({
    queries: teamIds.map((teamId) => ({
      queryKey: keys.teams.members(teamId, true),
      enabled,
      retry: settleStatusError,
      queryFn: () =>
        request(
          api.GET("/api/v1/teams/{team_id}/members", {
            params: { path: { team_id: teamId }, query: { include_descendants: true } },
          }),
          "could not load this team's roster",
        ),
    })),
    combine: (results) => ({
      members: results.flatMap((r) => r.data ?? []),
      pending: results.some((r) => r.isPending && r.fetchStatus !== "idle"),
    }),
  });
}

/** Pending invitations to a team. Team-admin gated (same 403 contract as the roster). */
export function useTeamInvitations(teamId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.invitations.team(teamId),
    enabled: Boolean(teamId) && enabled,
    retry: settleStatusError,
    queryFn: () =>
      request(
        api.GET("/api/v1/teams/{team_id}/invitations", { params: { path: { team_id: teamId! } } }),
        "could not load this team's invitations",
      ),
  });
}

/** The caller's OWN pending invitations to join a team — the recipient side. */
export function useMyInvitations() {
  return useQuery({
    queryKey: keys.invitations.me,
    queryFn: () => request(api.GET("/api/v1/invitations/me"), "could not load your invitations"),
  });
}

// ---- mutations ------------------------------------------------------------
//
// No invalidation wiring here: the shared MutationCache policy (api/queryClient.ts)
// invalidates the whole cache after every mutation, which covers each cached view a
// team/membership/invitation change can affect (the tree, rosters, invitations, the
// dashboard) without any of them being enumerated.

export function useCreateTeamMutation() {
  return useMutation({
    mutationFn: (payload: TeamCreate) =>
      request(api.POST("/api/v1/teams", { body: payload }), "could not create the team"),
  });
}

export function useRenameTeamMutation() {
  return useMutation({
    mutationFn: ({ teamId, name }: { teamId: string; name: string }) =>
      request(
        api.PATCH("/api/v1/teams/{team_id}", { params: { path: { team_id: teamId } }, body: { name } }),
        "could not rename the team",
      ),
  });
}

export function useMoveTeamMutation() {
  return useMutation({
    mutationFn: ({ teamId, newParentTeamId }: { teamId: string; newParentTeamId: string }) =>
      request(
        api.POST("/api/v1/teams/{team_id}/move", {
          params: { path: { team_id: teamId } },
          body: { new_parent_team_id: newParentTeamId },
        }),
        "could not move the team",
      ),
  });
}

export function useDeleteTeamMutation() {
  return useMutation({
    mutationFn: (teamId: string) =>
      request(api.DELETE("/api/v1/teams/{team_id}", { params: { path: { team_id: teamId } } }), "could not delete the team"),
  });
}

export function useAddMemberMutation() {
  return useMutation({
    mutationFn: ({ teamId, userId, role }: { teamId: string; userId: string; role: TeamRole }) =>
      request(
        api.POST("/api/v1/teams/{team_id}/memberships", {
          params: { path: { team_id: teamId } },
          body: { user_id: userId, team_id: teamId, role },
        }),
        "could not add the member",
      ),
  });
}

export function useChangeRoleMutation() {
  return useMutation({
    mutationFn: ({ teamId, userId, role }: { teamId: string; userId: string; role: TeamRole }) =>
      request(
        api.PATCH("/api/v1/teams/{team_id}/memberships/{user_id}", {
          params: { path: { team_id: teamId, user_id: userId } },
          body: { role },
        }),
        "could not change the role",
      ),
  });
}

export function useRemoveMemberMutation() {
  return useMutation({
    mutationFn: ({ teamId, userId }: { teamId: string; userId: string }) =>
      request(
        api.DELETE("/api/v1/teams/{team_id}/memberships/{user_id}", {
          params: { path: { team_id: teamId, user_id: userId } },
        }),
        "could not remove the member",
      ),
  });
}

export function useMoveMemberMutation() {
  return useMutation({
    mutationFn: ({ teamId, userId, targetTeamId, role }: { teamId: string; userId: string; targetTeamId: string; role?: TeamRole }) =>
      request(
        api.POST("/api/v1/teams/{team_id}/memberships/{user_id}/move", {
          params: { path: { team_id: teamId, user_id: userId } },
          body: { target_team_id: targetTeamId, role: role ?? null },
        }),
        "could not move the member",
      ),
  });
}

export function useCreateInvitationMutation() {
  return useMutation({
    mutationFn: ({ teamId, email, role }: { teamId: string; email: string; role: TeamRole }) =>
      request(
        api.POST("/api/v1/teams/{team_id}/invitations", { params: { path: { team_id: teamId } }, body: { email, role } }),
        "could not send the invitation",
      ),
  });
}

export function useRevokeInvitationMutation() {
  return useMutation({
    mutationFn: ({ teamId, invitationId }: { teamId: string; invitationId: string }) =>
      request(
        api.DELETE("/api/v1/teams/{team_id}/invitations/{invitation_id}", {
          params: { path: { team_id: teamId, invitation_id: invitationId } },
        }),
        "could not revoke the invitation",
      ),
  });
}

/** Accept an invitation addressed to me — joins the team (and its ancestor chain). */
export function useAcceptInvitationMutation() {
  return useMutation({
    mutationFn: (invitationId: string) =>
      request(
        api.POST("/api/v1/invitations/{invitation_id}/accept", { params: { path: { invitation_id: invitationId } } }),
        "could not accept the invitation",
      ),
  });
}

/** Decline an invitation addressed to me. */
export function useRejectInvitationMutation() {
  return useMutation({
    mutationFn: (invitationId: string) =>
      request(
        api.POST("/api/v1/invitations/{invitation_id}/reject", { params: { path: { invitation_id: invitationId } } }),
        "could not decline the invitation",
      ),
  });
}

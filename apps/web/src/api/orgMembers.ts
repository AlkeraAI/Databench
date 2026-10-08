// React Query hooks for the org admin's MEMBER surface: the account controls
// (deactivation, SSO break-glass). The per-member budget allocations are
// billing's (api/memberBudgets.ts). Member management is part of the Teams
// page's member drawer, so these hooks sit beside api/teams.ts rather than
// behind a separate members page. Every endpoint here is org-admin gated
// server-side; callers gate the UI on `is_org_admin` (api/dashboard).

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "./client";
import { keys } from "./keys";

export type OrgMember = components["schemas"]["OrgMemberRead"];

// ---- account controls -------------------------------------------------------

/** Every member of the caller's org with their account flags (active, SSO
 *  break-glass). `enabled` gates the fetch so a drawer only queries while open. */
export function useOrgMembers(enabled = true) {
  return useQuery({
    queryKey: keys.orgAdmin.members,
    enabled,
    queryFn: () => request(api.GET("/api/v1/org/members"), "could not load your members"),
  });
}

/** Deactivate (offboard: sign-in blocked, data kept) or reactivate a member. */
export function useSetMemberActive() {
  return useMutation({
    mutationFn: ({ userId, active }: { userId: string; active: boolean }) =>
      request(
        api.PUT("/api/v1/org/members/{user_id}/active", {
          params: { path: { user_id: userId } },
          body: { active },
        }),
        "could not update the member",
      ),
  });
}

/** SSO break-glass: let an account sign in with a password even when single
 *  sign-on is required. */
export function useSetSsoExemption() {
  return useMutation({
    mutationFn: ({ userId, exempt }: { userId: string; exempt: boolean }) =>
      request(
        api.PUT("/api/v1/org/sso/exemptions/{user_id}", {
          params: { path: { user_id: userId } },
          body: { exempt },
        }),
        "could not update the exemption",
      ),
  });
}

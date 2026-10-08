// React Query hooks for the account/Profile settings surface. Every piece maps to a real backend
// endpoint already typed in @alkera/sdk — there is no mock data behind this page:
//
//   PATCH /api/v1/users/{id}        → update first/last/email (UserUpdate)
//   GET   /api/v1/auth/identities   → linked OAuth providers (LinkedIdentityList)
//   GET   /api/v1/auth/sessions     → active sessions + CLI tokens (SessionListResponse)
//   DELETE /api/v1/auth/sessions/{jti} → revoke one
//   POST  /api/v1/auth/logout-all   → revoke every OTHER session
//   POST  /api/v1/auth/password-reset/request → the "change password" link (see api/auth.ts)
//
// The cookie rides every request (the SDK defaults credentials:"include"). The shared MutationCache
// policy (api/queryClient.ts) refetches after every mutation, so the UI reflects the server without
// any per-hook invalidation wiring.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { meKey } from "./auth";
import { api, request } from "./client";
import { keys } from "./keys";

export type LinkedIdentity = components["schemas"]["LinkedIdentity"];
export type Session = components["schemas"]["SessionRead"];
export type UserUpdate = components["schemas"]["UserUpdate"];
type UserRead = components["schemas"]["UserRead"];
type OrgUserRead = components["schemas"]["OrgUserRead"];

/** The PATCH answers with the caller's full account on a self edit and with the
 *  org's narrow view of a colleague on an admin's edit; only the first carries the
 *  account's verification state. */
function isOwnAccount(read: UserRead | OrgUserRead): read is UserRead {
  return "email_verification_required" in read;
}

/** External-login providers linked to the caller (no secrets) — view-only. */
export function useIdentities(enabled = true) {
  return useQuery({
    queryKey: keys.auth.identities,
    queryFn: () => request(api.GET("/api/v1/auth/identities"), "could not load your sign-in providers"),
    select: (d) => d.identities,
    enabled,
  });
}

/** The caller's active sessions + CLI tokens, each revocable. */
export function useSessions(enabled = true) {
  return useQuery({
    queryKey: keys.auth.sessions,
    queryFn: () => request(api.GET("/api/v1/auth/sessions"), "could not load your sessions"),
    select: (d) => d.sessions,
    enabled,
  });
}

/** Patch the caller's own profile (first/last/email). Reseeds the cached user on success. */
export function useUpdateProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ userId, patch }: { userId: string; patch: UserUpdate }): Promise<UserRead> => {
      const saved = await request(
        api.PATCH("/api/v1/users/{user_id}", { params: { path: { user_id: userId } }, body: patch }),
        "could not save your profile",
      );
      if (!isOwnAccount(saved)) throw new Error("could not save your profile");
      return saved;
    },
    onSuccess: (user) => {
      qc.setQueryData(meKey, user);
    },
  });
}

/** Revoke a single session/token by its jti. */
export function useRevokeSession() {
  return useMutation({
    mutationFn: (jti: string) =>
      request(
        api.DELETE("/api/v1/auth/sessions/{jti}", { params: { path: { jti } } }),
        "could not revoke that session",
      ),
  });
}

/** Revoke every session except the current one ("sign out everywhere"). */
export function useLogoutAll() {
  return useMutation({
    mutationFn: () => request(api.POST("/api/v1/auth/logout-all"), "could not sign out your other sessions"),
  });
}

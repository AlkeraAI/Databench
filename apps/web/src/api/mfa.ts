// React Query hooks for two-factor authentication (TOTP), wired to the real
// backend and typed through @alkera/sdk. The cookie rides every request.
//
//   GET  /api/v1/auth/mfa/status   → { enabled, backup_codes_remaining }
//   POST /api/v1/auth/mfa/enroll   → { secret, otpauth_uri } (pending, not yet active)
//   POST /api/v1/auth/mfa/confirm  → { backup_codes } (activates, shown once)
//   POST /api/v1/auth/mfa/disable  → turns it off (requires a current code)
//
// Confirm/disable ride the shared MutationCache policy (api/queryClient.ts), which
// refreshes the status query AND the cached identity (with everything else) so the
// profile surface and any `user`-derived gate reflect the change.

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "./client";
import { keys } from "./keys";

export type MfaStatus = components["schemas"]["MfaStatusResponse"];
export type MfaEnroll = components["schemas"]["MfaEnrollResponse"];

/** Whether MFA is on for the caller, and how many backup codes remain. */
export function useMfaStatus(enabled = true) {
  return useQuery({
    queryKey: keys.auth.mfaStatus,
    queryFn: () => request(api.GET("/api/v1/auth/mfa/status"), "could not load your two-factor status"),
    enabled,
  });
}

/** Begin enrollment — mints the pending secret + otpauth URI for the authenticator app.
 *  A session older than the server's freshness window must re-present the account password
 *  (`current_password_required`); the caller collects it and retries with it.
 *  Nothing cached changes until confirm, so it opts out of the shared invalidation. */
export function useMfaEnroll() {
  return useMutation({
    mutationFn: (currentPassword?: string) =>
      request(
        api.POST("/api/v1/auth/mfa/enroll", {
          body: currentPassword === undefined ? {} : { current_password: currentPassword },
        }),
        "could not start two-factor setup",
      ),
    meta: { invalidates: "none" },
  });
}

/** Confirm a code to activate MFA. Returns the single-use backup codes (shown once). */
export function useMfaConfirm() {
  return useMutation({
    mutationFn: (code: string) =>
      request(api.POST("/api/v1/auth/mfa/confirm", { body: { code } }), "that code didn't match"),
  });
}

/** Turn MFA off — requires a current code, so a hijacked session can't disable it freely. */
export function useMfaDisable() {
  return useMutation({
    mutationFn: (code: string) =>
      request(api.POST("/api/v1/auth/mfa/disable", { body: { code } }), "could not turn off two-factor"),
  });
}

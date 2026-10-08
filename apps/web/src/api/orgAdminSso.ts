// React Query hooks for the org-admin SSO surface — the IdP connection (OIDC /
// SAML) + SCIM provisioning. Real endpoints, typed through @alkera/sdk:
//
//   GET    /api/v1/org/sso              → SsoConnectionRead
//   PUT    /api/v1/org/sso              → SsoConnectionUpdateRequest (full replace; the email
//                                          domains are assigned by Alkera staff, not here)
//   POST   /api/v1/org/sso/scim-token   → mint a SCIM bearer token (shown once)
//   DELETE /api/v1/org/sso/scim-token   → revoke + disable SCIM
//
// The config mutations return the fresh connection; we seed the cache. Mint rides
// the shared MutationCache policy, which refetches so `has_scim_token` flips on
// (the token itself lives in component state, never the query cache).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "./client";
import { keys } from "./keys";

export type SsoConnection = components["schemas"]["SsoConnectionRead"];
export type SsoUpdate = components["schemas"]["SsoConnectionUpdateRequest"];
export type ScimToken = components["schemas"]["ScimTokenResponse"];

/** The org's IdP connection + SCIM state. */
export function useSsoConfig(enabled = true) {
  return useQuery({
    queryKey: keys.orgAdmin.sso,
    queryFn: () => request(api.GET("/api/v1/org/sso"), "could not load SSO settings"),
    enabled,
  });
}

/** Replace the IdP connection config (protocol, credentials, domains, mapping). */
export function useUpdateSsoConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SsoUpdate) =>
      request(api.PUT("/api/v1/org/sso", { body }), "could not save SSO settings"),
    onSuccess: (data) => qc.setQueryData(keys.orgAdmin.sso, data),
  });
}

/** Mint a new SCIM bearer token — returned once, must be copied immediately. */
export function useMintScimToken() {
  return useMutation({
    mutationFn: () => request(api.POST("/api/v1/org/sso/scim-token"), "could not mint a SCIM token"),
  });
}

/** Revoke the active SCIM token and disable provisioning. Returns the fresh config. */
export function useRevokeScimToken() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => request(api.DELETE("/api/v1/org/sso/scim-token"), "could not revoke the SCIM token"),
    onSuccess: (data) => qc.setQueryData(keys.orgAdmin.sso, data),
  });
}

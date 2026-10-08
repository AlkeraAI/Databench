// An org's compute grants — the platform's standing permission for a tenant to
// run machines. Without one the customer's box answers 429 no_compute_grant, so
// granting is the last step of onboarding a new org. Staff read; only
// ALKERA_ADMIN may write or revoke (the backend enforces the grade itself, the
// UI only hides what a support-grade staffer cannot use).

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "../client";
import { keys } from "../keys";

export type ComputeGrant = components["schemas"]["ComputeGrantRead"];
export type ComputeGrantUpsert = components["schemas"]["ComputeGrantUpsertRequest"];

/** Every LIVE grant anywhere in the org's tree, newest first. */
export function useOrgComputeGrants(orgId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.admin.computeOrg(orgId),
    enabled: orgId !== undefined && enabled,
    queryFn: () =>
      request(
        api.GET("/admin/v1/orgs/{org_id}/compute", { params: { path: { org_id: orgId! } } }),
        "could not load this org's compute grants",
      ),
  });
}

/** Grant the org compute, or bring its live grant up to date (idempotent on the
 *  team + machine-type pair, so a second grant raises the ceiling rather than
 *  stacking a second row). */
export function useGrantOrgComputeMutation(orgId: string | undefined) {
  return useMutation({
    mutationFn: (body: ComputeGrantUpsert) =>
      request(
        api.PUT("/admin/v1/orgs/{org_id}/compute", {
          params: { path: { org_id: orgId! } },
          body,
        }),
        "could not grant compute",
      ),
    meta: { invalidates: [keys.admin.computeOrg(orgId)] },
  });
}

/** Revoke one grant. The org in the path is the tenancy guard: a grant id from
 *  another org reads as not-found rather than being revoked through this URL. */
export function useRevokeOrgComputeMutation(orgId: string | undefined) {
  return useMutation({
    mutationFn: (grantId: string) =>
      request(
        api.DELETE("/admin/v1/orgs/{org_id}/compute/{grant_id}", {
          params: { path: { org_id: orgId!, grant_id: grantId } },
        }),
        "could not revoke this grant",
      ),
    meta: { invalidates: [keys.admin.computeOrg(orgId)] },
  });
}

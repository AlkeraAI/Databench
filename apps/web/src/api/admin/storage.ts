// React Query hooks for the platform admin's org STORAGE ceiling — the one place
// an operator overrides what a plan includes, which is how an Enterprise org
// (whose plan sets no figure at all) gets a ceiling in the first place.
//
// Three verbs, deliberately distinct: PUT a figure caps the org, PUT `null` is an
// explicit "unlimited" (still an override — the plan no longer applies), DELETE
// hands the org back to its plan's figure. A `null` body and no override are NOT
// the same state, so the hooks keep them separate rather than folding both into
// one "clear".

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { QUERY_RETRY_ATTEMPTS } from "@/lib/limits";

import { api, request } from "../client";
import { ApiError } from "../errors";
import { keys } from "../keys";

export type OrgStorage = components["schemas"]["OrgStorageRead"];

/** Where an org's ceiling comes from, as the register reads it. */
export type StorageLimitSource = OrgStorage["storage_limit_source"];

/** A refusal is an answer: a staffer the endpoint does not serve gets the same 403 however often
 *  they ask, so it settles at once like a 401 or a 404 rather than walking the retry ladder. */
const settleRefusal = (count: number, err: unknown): boolean =>
  !(err instanceof ApiError && [401, 403, 404, 410].includes(err.status)) && count < QUERY_RETRY_ATTEMPTS;

/** One org's ceiling, its source, and its usage. */
export function useAdminOrgStorage(orgId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.admin.orgStorage(orgId),
    enabled: Boolean(orgId) && enabled,
    retry: settleRefusal,
    queryFn: () =>
      request(
        api.GET("/admin/v1/orgs/{org_id}/storage", { params: { path: { org_id: orgId! } } }),
        "could not load this org's storage",
      ),
  });
}

/** Write the org's ceiling by hand. `limitBytes: null` is an explicit unlimited. */
export function useSetAdminOrgStorage(orgId: string) {
  return useMutation({
    mutationFn: (limitBytes: number | null) =>
      request(
        api.PUT("/admin/v1/orgs/{org_id}/storage", {
          params: { path: { org_id: orgId } },
          body: { limit_bytes: limitBytes },
        }),
        "could not set this org's storage limit",
      ),
  });
}

/** Drop the override — the org falls back to its plan's figure. Idempotent. */
export function useClearAdminOrgStorage(orgId: string) {
  return useMutation({
    mutationFn: () =>
      request(
        api.DELETE("/admin/v1/orgs/{org_id}/storage", { params: { path: { org_id: orgId } } }),
        "could not restore this org's plan storage",
      ),
  });
}

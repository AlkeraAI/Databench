// React Query hooks for the org-admin Settings surface. Real endpoints, no mock data:
//
//   GET /api/v1/org/settings        → sign-in-method toggles (OrgSettingsRead)
//   PUT /api/v1/org/settings        → OrgSettingsUpdate (partial)
//
// The org's read-only identity (name, id, member count) comes from /api/v1/dashboard
// (useIdentityDashboard in api/dashboard.ts) — its `org` is the root TeamRead.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "./client";
import { keys } from "./keys";

export type OrgSettings = components["schemas"]["OrgSettingsRead"];
export type OrgSettingsUpdate = components["schemas"]["OrgSettingsUpdate"];

/** The two sign-in-method toggles an org admin controls. */
export function useOrgSettings(enabled = true) {
  return useQuery({
    queryKey: keys.org.settings,
    queryFn: () => request(api.GET("/api/v1/org/settings"), "could not load sign-in methods"),
    enabled,
  });
}

/** Apply a partial change to the sign-in-method toggles (optimistic at the call site). */
export function useUpdateOrgSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: OrgSettingsUpdate) =>
      request(api.PUT("/api/v1/org/settings", { body: patch }), "could not save sign-in methods"),
    onSuccess: (data) => qc.setQueryData(keys.org.settings, data),
  });
}

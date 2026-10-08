// React Query hooks for an org's live-editing switch: whether its files open as
// co-edited live documents. The org's own setting is `true`, `false`, or `null`
// (follow the deployment's LIVE_EDITING_ENABLED). Reading is staff-grade,
// writing is platform-admin, both decided by the route.

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { QUERY_RETRY_ATTEMPTS } from "@/lib/limits";

import { api, request } from "../client";
import { ApiError } from "../errors";
import { keys } from "../keys";

export type OrgLiveEditing = components["schemas"]["OrgLiveEditingRead"];

const settleRefusal = (count: number, err: unknown): boolean =>
  !(err instanceof ApiError && [401, 403, 404, 410].includes(err.status)) && count < QUERY_RETRY_ATTEMPTS;

/** Where one org stands: the deployment's default, its own setting, the result. */
export function useAdminOrgLiveEditing(orgId: string | undefined) {
  return useQuery({
    queryKey: keys.admin.orgLiveEditing(orgId),
    enabled: Boolean(orgId),
    retry: settleRefusal,
    queryFn: () =>
      request(
        api.GET("/admin/v1/orgs/{org_id}/live-editing", { params: { path: { org_id: orgId! } } }),
        "could not load this org's live editing",
      ),
  });
}

/** Set (`true` / `false`) or clear (`null`) the org's own setting. */
export function useSetAdminOrgLiveEditing(orgId: string) {
  return useMutation({
    mutationFn: (enabled: boolean | null) =>
      request(
        api.PUT("/admin/v1/orgs/{org_id}/live-editing", {
          params: { path: { org_id: orgId } },
          body: { enabled },
        }),
        "could not change this org's live editing",
      ),
    meta: { invalidates: [keys.admin.orgLiveEditing(orgId)] },
  });
}

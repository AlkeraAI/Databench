// React Query hooks for the org-admin deployment health tab (self-hosted only).
// The GET returns the latest snapshot (with the schedule-liveness check overlaid at
// read time); the manual run executes the checks inline and returns a fresh report.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "./client";
import { keys } from "./keys";

export type DeploymentHealthReport = components["schemas"]["DeploymentHealthReport"];
export type DeploymentHealthCheck = components["schemas"]["DeploymentHealthCheckRead"];

export function useDeploymentHealth(enabled = true) {
  return useQuery({
    queryKey: keys.orgAdmin.deploymentHealth,
    enabled,
    // A dashboard the operator glances at — refetch on focus, keep it fresh-ish.
    refetchOnWindowFocus: true,
    staleTime: 30_000,
    queryFn: () =>
      request(api.GET("/api/v1/org/deployment-health"), "could not load deployment health"),
  });
}

export function useRunDeploymentHealth() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      request(api.POST("/api/v1/org/deployment-health/run"), "could not run the health checks"),
    onSuccess: (data) => qc.setQueryData(keys.orgAdmin.deploymentHealth, data),
  });
}

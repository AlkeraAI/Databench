// React Query hooks for the org-admin BYOK provider credentials (self-hosted,
// entitled). Secrets are write-only: responses never carry a key, only a masked
// hint + verification status. The routes 404 when the BYOK feature is unentitled.

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "./client";
import { keys } from "./keys";

export type ModelProvider = components["schemas"]["ModelProviderRead"];
export type ModelProviderUpdate = components["schemas"]["ModelProviderUpdateRequest"];
export type ModelProviderTestResult = components["schemas"]["ModelProviderTestResult"];
export type ProviderName = ModelProvider["provider"];

export function useModelProviders(enabled = true) {
  return useQuery({
    queryKey: keys.orgAdmin.modelProviders,
    enabled,
    queryFn: () =>
      request(api.GET("/api/v1/org/model-providers"), "could not load provider settings"),
  });
}

export function useUpdateModelProvider() {
  return useMutation({
    mutationFn: ({ provider, body }: { provider: ProviderName; body: ModelProviderUpdate }) =>
      request(
        api.PUT("/api/v1/org/model-providers/{provider}", {
          params: { path: { provider } },
          body,
        }),
        "could not save the provider",
      ),
  });
}

export function useRemoveModelProvider() {
  return useMutation({
    mutationFn: (provider: ProviderName) =>
      request(
        api.DELETE("/api/v1/org/model-providers/{provider}", { params: { path: { provider } } }),
        "could not remove the provider",
      ),
  });
}

export function useTestModelProvider() {
  return useMutation({
    mutationFn: (provider: ProviderName) =>
      request(
        api.POST("/api/v1/org/model-providers/{provider}/test", {
          params: { path: { provider } },
        }),
        "could not test the connection",
      ),
  });
}

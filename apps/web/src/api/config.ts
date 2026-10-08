import { useQuery } from "@tanstack/react-query";
import { currentBrand } from "@alkera/ui";
import type { components } from "@alkera/sdk";
import { api } from "./client";
import { keys } from "./keys";

export type PublicConfig = components["schemas"]["PublicConfigResponse"];

// The fallback if the request fails: the build's brand, and fail-closed flags. A
// white-labeled deployment overrides these on the backend; on a successful fetch
// the served values win, so one web image can run under any brand. A function, not a
// constant, so reading the brand waits until the product has registered its own.
export const defaultPublicConfig = (): PublicConfig => ({
  product_name: currentBrand().productName,
  support_email: null,
  // Fail-closed fallback while the request is in flight / on error — assume no
  // telemetry until a reachable backend explicitly enables it.
  telemetry_enabled: false,
  // Fail-closed: assume SaaS (hide the self-hosted Deployment nav) until a
  // reachable backend says otherwise.
  self_hosted: false,
  // Fail-closed: a workspace holds one chat until a reachable backend says otherwise.
  workspaces_multi_chat: false,
  // Fail-closed: no multi-org door shows until a reachable backend says the
  // feature is on.
  multi_org_enabled: false,
});

/**
 * Public (unauthenticated) branding the SPA needs before login — product name +
 * support email. Falls back to the build's brand on any error so the UI never
 * renders an empty brand.
 */
export function usePublicConfig() {
  return useQuery({
    queryKey: keys.publicConfig,
    queryFn: async (): Promise<PublicConfig> => {
      const result = await api.GET("/api/v1/config");
      if (!result.response.ok || !result.data) return defaultPublicConfig();
      return result.data;
    },
    staleTime: 5 * 60_000,
  });
}

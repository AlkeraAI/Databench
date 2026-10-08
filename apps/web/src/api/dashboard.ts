// React Query hooks for the dashboard's own data: the identity context the
// overview greets with (/api/v1/dashboard). The connections count and the
// recent-chat list come from the same hooks their own pages use
// (api/connections.ts, api/chats.ts), one spelling per read. The plan, usage and
// storage reads are billing's (api/myBilling.ts).

import { useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "./client";
import { keys } from "./keys";

export type IdentityDashboard = components["schemas"]["DashboardResponse"];
/** A connection the caller can use — their own, or one of their teams'. */
export type MyConnection = components["schemas"]["TeamConnectionRead"];

// The dashboard's live-source queries. When a fixture is seeded through the seam, no live writer is
// mounted, so these hooks never run — network suppression on the injected/preview path is structural,
// not a per-query `enabled` flag.

/** Identity context — the user's display name + org name for the greeting. */
export function useIdentityDashboard() {
  return useQuery({
    queryKey: keys.dashboard.identity,
    queryFn: () => request(api.GET("/api/v1/dashboard"), "could not load your dashboard"),
  });
}


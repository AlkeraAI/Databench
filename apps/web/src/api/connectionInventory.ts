import { useQuery } from "@tanstack/react-query";

import { api, request } from "./client";
import { keys } from "./keys";

// Spelled locally rather than read off the generated SDK schema: the SDK regenerates
// after this endpoint's OpenAPI lands, and until then these fields ARE the wire shape
// (the backend's connection inventory schema). Re-point to
// components["schemas"]["ConnectionInventoryRead"] at the next SDK regeneration.
export interface ConnectionInventoryRow {
  user_id: string;
  user_name: string;
  user_email: string;
  plugin: string;
  status: string;
  last_verified_at: string | null;
  reported_at: string;
}

interface ConnectionInventoryResponse {
  connections: ConnectionInventoryRow[];
}

/** The caller's own connections, as their machines last reported them. */
export function useMyConnectionInventory() {
  return useQuery({
    queryKey: keys.connectionInventory.me,
    queryFn: () =>
      request<ConnectionInventoryResponse>(
        api.GET("/api/v1/me/connection-inventory" as never) as never,
        "Your connections could not be loaded",
      ),
  });
}

/** Every member's locally added connections. Org admins only -- the page mounts
 *  this hook only for an admin, so a member never fires a call that 403s. */
export function useOrgConnectionInventory() {
  return useQuery({
    queryKey: keys.connectionInventory.org,
    queryFn: () =>
      request<ConnectionInventoryResponse>(
        api.GET("/api/v1/org/connection-inventory" as never) as never,
        "The organization's connections could not be loaded",
      ),
  });
}

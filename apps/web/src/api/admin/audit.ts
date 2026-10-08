// React Query hook for the platform audit log — the bound chronicle of every
// successful Alkera support & admin action. Paginated server-side (page +
// page_size). Platform-ADMIN gated (the highest grade), so the page that reads it
// sits behind RequirePlatformAdmin.

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "../client";
import { keys } from "../keys";
import type { AuditFilters } from "../orgAdminAudit";

export type AuditLogPage = components["schemas"]["AuditLogPage"];
export type AuditLogEntry = components["schemas"]["AuditLogRead"];

/** One page of the audit log under `filters` (an exact action, the actor's email, a time range).
 *  `keepPreviousData` holds the prior page on screen while the next loads, so paging and
 *  filtering don't flash the skeleton. */
export function useAuditLogs(page: number, pageSize: number, filters: AuditFilters = {}, enabled = true) {
  return useQuery({
    queryKey: keys.admin.auditLogs(page, pageSize, filters),
    placeholderData: keepPreviousData,
    enabled,
    queryFn: () =>
      request(
        api.GET("/admin/v1/audit-logs", { params: { query: { page, page_size: pageSize, ...filters } } }),
        "could not load the audit log",
      ),
  });
}

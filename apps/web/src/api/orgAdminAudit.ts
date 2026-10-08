// React Query hooks for the org-admin audit log — security-relevant actions in
// the org (logins, billing changes, SSO config, agent activity). Real endpoints,
// typed through @alkera/sdk:
//
//   GET /api/v1/org/audit-events?offset&limit&<filters>   → OrgAuditPage
//   GET /api/v1/org/audit-events/verify                   → AuditChainVerification
//   GET /api/v1/org/audit-events/export.csv?<filters>     → CSV download (direct link)
//
// Pagination is offset-based and managed by the page.

import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, apiBaseUrl, request } from "./client";
import { keys } from "./keys";

export type OrgAuditPage = components["schemas"]["OrgAuditPage"];
export type OrgAuditEvent = components["schemas"]["OrgAuditEventRead"];
export type AuditChainVerification = components["schemas"]["AuditChainVerification"];

/** Server-side narrowing, shared by the list and the CSV export. `action` is a
 *  prefix (`agent.` selects the whole family); the time bounds are inclusive
 *  ISO timestamps. */
export type AuditFilters = {
  action?: string;
  actor_email?: string;
  created_after?: string;
  created_before?: string;
};

/** One page of audit events at `offset`, `limit` per page, under `filters`. */
export function useOrgAudit(offset: number, limit = 50, filters: AuditFilters = {}, enabled = true) {
  return useQuery({
    queryKey: keys.orgAdmin.audit(offset, limit, filters),
    queryFn: () =>
      request(
        api.GET("/api/v1/org/audit-events", { params: { query: { offset, limit, ...filters } } }),
        "could not load the audit log",
      ),
    // Hold the previous page while a filter/offset change refetches — no loading flash.
    placeholderData: keepPreviousData,
    enabled,
  });
}

/** On-demand tamper check of the whole org chain (never automatic — it walks
 *  every event). An imperative run, so it's a mutation over the GET; it changes
 *  nothing server-side, so it invalidates nothing. */
export function useAuditVerify() {
  return useMutation({
    mutationFn: () =>
      request(api.GET("/api/v1/org/audit-events/verify"), "could not verify the audit chain"),
    meta: { invalidates: "none" },
  });
}

/** The CSV export is a plain authenticated GET — the browser downloads it (the
 *  session cookie rides along same-site), so it's a direct link, carrying the
 *  same filters as the visible list. */
export function auditCsvUrl(filters: AuditFilters = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
  const query = params.toString();
  return `${apiBaseUrl}/api/v1/org/audit-events/export.csv${query ? `?${query}` : ""}`;
}

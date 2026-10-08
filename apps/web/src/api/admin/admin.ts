// React Query hooks for the platform-admin registers: organizations, users, and
// platform-role grants. Every shape is the SDK's generated type, so a page never
// hand-types a response. The endpoints sit under `/admin/v1/...` and are
// platform-staff gated server-side; the route guards (RequirePlatformStaff /
// RequirePlatformAdmin) decide who can reach the page, and a 403 from a staff
// member who lacks the higher grade still surfaces as an ApiError the page can show.
//
// Mutations ride the shared MutationCache policy (api/queryClient.ts), so a rename,
// a new org, or a role change refreshes every cached list + detail at once.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "../client";
import { keys } from "../keys";

export type Org = components["schemas"]["OrgRead"];
export type OrgCreate = components["schemas"]["OrgCreate"];
export type AdminUser = components["schemas"]["AdminUserRow"];
export type AdminUserIpInfo = components["schemas"]["AdminUserIpInfoRead"];
export type AdminTeam = components["schemas"]["TeamRead"];
export type AdminOrgMember = components["schemas"]["TeamMemberRead"];
export type OrgSettings = components["schemas"]["OrgSettingsRead"];
export type PlatformRole = components["schemas"]["PlatformRole"];

// generated after landing: the sandbox limits ride the admin settings read and
// write; declared here until the SDK is regenerated.
export type AdminOrgSettings = OrgSettings & {
  sandbox_vcpu?: number | null;
  sandbox_memory_mb?: number | null;
};
export type AdminOrgSettingsPatch = Partial<{
  allow_login_google: boolean;
  allow_login_github: boolean;
  sandbox_vcpu: number | null;
  sandbox_memory_mb: number | null;
}>;

// The org activity reads (`/admin/v1/orgs/{id}/chats|errors|audit`).
export type OrgChatInsight = components["schemas"]["OrgChatInsight"];
export type OrgIssue = components["schemas"]["OrgIssue"];
export type OrgAuditEvent = components["schemas"]["OrgAuditEventRead"];

// `GET /admin/v1/ops/summary`.
export type OpsCount = components["schemas"]["OpsCount"];
export type OpsSummary = components["schemas"]["OpsSummary"];

/** How often the ops page re-reads the summary. */
export const OPS_REFRESH_MS = 30_000;

/** A GET for a route the generated client does not know yet, through the same
 *  client (so the session refresh on a 401 still applies) and the same
 *  `request`, so a non-2xx is the same `ApiError` every other hook throws. */
const untyped = api as unknown as {
  GET: (path: string, init?: { params: { path: Record<string, string>; query?: Record<string, number> } }) => Promise<{
    data?: unknown;
    error?: unknown;
    response: Response;
  }>;
};

function getUntyped<T>(path: string, orgId: string, limit: number) {
  return untyped.GET(path, { params: { path: { org_id: orgId }, query: { limit } } }) as Promise<{
    data?: T;
    error?: unknown;
    response: Response;
  }>;
}

// ---- organizations --------------------------------------------------------

/** Every organization on the platform, each carrying its materialized member count. */
export function useAdminOrgs(enabled = true) {
  return useQuery({
    queryKey: keys.admin.orgs,
    queryFn: () => request(api.GET("/admin/v1/orgs"), "could not load organizations"),
    enabled,
  });
}

/** One organization by id. */
export function useAdminOrg(orgId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.admin.org(orgId),
    enabled: Boolean(orgId) && enabled,
    queryFn: () =>
      request(api.GET("/admin/v1/orgs/{org_id}", { params: { path: { org_id: orgId! } } }), "could not load this organization"),
  });
}

/** The org's team graph (for the detail page's teams tab). */
export function useAdminOrgTeams(orgId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.admin.orgTeams(orgId),
    enabled: Boolean(orgId) && enabled,
    queryFn: () =>
      request(api.GET("/admin/v1/orgs/{org_id}/teams", { params: { path: { org_id: orgId! } } }), "could not load this org's teams"),
  });
}

/** The org's flat member roster (everyone materialized onto the org root). */
export function useAdminOrgMembers(orgId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.admin.orgMembers(orgId),
    enabled: Boolean(orgId) && enabled,
    queryFn: () =>
      request(api.GET("/admin/v1/orgs/{org_id}/members", { params: { path: { org_id: orgId! } } }), "could not load this org's members"),
  });
}

/** The org's login settings (OAuth providers). Platform-admin gated. */
export function useAdminOrgSettings(orgId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.admin.orgSettings(orgId),
    enabled: Boolean(orgId) && enabled,
    queryFn: async (): Promise<AdminOrgSettings> =>
      request(api.GET("/admin/v1/orgs/{org_id}/settings", { params: { path: { org_id: orgId! } } }), "could not load this org's settings"),
  });
}

/** The org's chats, most recently active first, with the machine serving each. */
export function useAdminOrgChats(orgId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.admin.orgChats(orgId),
    enabled: Boolean(orgId) && enabled,
    queryFn: () =>
      request(getUntyped<{ items: OrgChatInsight[] }>("/admin/v1/orgs/{org_id}/chats", orgId!, 50), "could not load this org's chats"),
  });
}

/** The org's errors and refusals from the last week, newest first. */
export function useAdminOrgErrors(orgId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.admin.orgErrors(orgId),
    enabled: Boolean(orgId) && enabled,
    queryFn: () =>
      request(getUntyped<{ items: OrgIssue[] }>("/admin/v1/orgs/{org_id}/errors", orgId!, 50), "could not load this org's errors"),
  });
}

/** The org's most recent audit events. */
export function useAdminOrgAudit(orgId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.admin.orgAudit(orgId),
    enabled: Boolean(orgId) && enabled,
    queryFn: () =>
      request(
        getUntyped<{ items: OrgAuditEvent[]; total: number }>("/admin/v1/orgs/{org_id}/audit", orgId!, 50),
        "could not load this org's audit trail",
      ),
  });
}

/** SaaS-wide health: machines, chats served, spend against the cap, crash reports, deployment checks. */
export function useOpsSummary() {
  return useQuery({
    queryKey: keys.admin.opsSummary,
    refetchInterval: OPS_REFRESH_MS,
    queryFn: () =>
      request(untyped.GET("/admin/v1/ops/summary") as Promise<{ data?: OpsSummary; error?: unknown; response: Response }>, "could not load the ops summary"),
  });
}

export function useCreateOrgMutation() {
  return useMutation({
    mutationFn: (body: OrgCreate) => request(api.POST("/admin/v1/orgs", { body }), "could not create the organization"),
  });
}

export function useRenameOrgMutation() {
  return useMutation({
    mutationFn: ({ orgId, name }: { orgId: string; name: string }) =>
      request(api.PATCH("/admin/v1/orgs/{org_id}", { params: { path: { org_id: orgId } }, body: { name } }), "could not rename the organization"),
  });
}

export function useDeleteOrgMutation() {
  return useMutation({
    mutationFn: (orgId: string) =>
      request(api.DELETE("/admin/v1/orgs/{org_id}", { params: { path: { org_id: orgId } } }), "could not delete the organization"),
  });
}

export function useUpdateOrgSettingsMutation(orgId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: AdminOrgSettingsPatch): Promise<AdminOrgSettings> =>
      request(
        api.PUT("/admin/v1/orgs/{org_id}/settings", { params: { path: { org_id: orgId } }, body: patch }),
        "could not update the settings",
      ),
    onSuccess: (data) => {
      qc.setQueryData(keys.admin.orgSettings(orgId), data);
    },
  });
}

// ---- users ----------------------------------------------------------------

/** Every user on the platform, each carrying its platform role (if any). */
export function useAdminUsers(enabled = true) {
  return useQuery({
    queryKey: keys.admin.users,
    queryFn: () => request(api.GET("/admin/v1/users"), "could not load users"),
    enabled,
  });
}

/** Best-effort geo/network context for a user's recorded IPs (signup + last
 *  login). Degrades to an `error` field per IP when the lookup can't run. */
export function useUserIpInfo(userId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.admin.userIpInfo(userId),
    enabled: Boolean(userId) && enabled,
    queryFn: () =>
      request(
        api.GET("/admin/v1/users/{user_id}/ip-info", { params: { path: { user_id: userId! } } }),
        "could not look up IP info",
      ),
  });
}

/** Set (or clear) a user's platform role. `null` demotes to a regular user. */
export function useSetPlatformRoleMutation() {
  return useMutation({
    mutationFn: ({ userId, platformRole }: { userId: string; platformRole: PlatformRole | null }) =>
      request(
        api.PATCH("/admin/v1/users/{user_id}/platform_role", {
          params: { path: { user_id: userId } },
          body: { platform_role: platformRole },
        }),
        "could not update the platform role",
      ),
  });
}

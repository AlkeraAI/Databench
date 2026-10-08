import { Navigate, Outlet } from "react-router-dom";

import { useIdentityDashboard } from "../../api/dashboard";
import { GateError, GateLoading } from "./GateLoading";

/**
 * Gate the org-admin surfaces (org billing, members, SSO, audit log). Org-admin is
 * an ORG-scoped capability (Admin of the org root), surfaced on the dashboard
 * payload as `is_org_admin` — distinct from platform staff, which
 * `RequirePlatformStaff` checks. A non-admin is bounced to their dashboard. The
 * server enforces the same gate on every endpoint.
 */
export function RequireOrgAdmin() {
  const { data, isPending, isError, error, refetch } = useIdentityDashboard();
  if (isPending) return <GateLoading label="Checking your access" />;
  if (isError) return <GateError error={error} onRetry={() => void refetch()} />;
  if (!data?.is_org_admin) return <Navigate to="/" replace />;
  return <Outlet />;
}

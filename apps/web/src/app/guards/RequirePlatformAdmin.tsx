import { Navigate, Outlet } from "react-router-dom";

import { useCurrentUser } from "../../api/auth";
import { GateError, GateLoading } from "./GateLoading";

/**
 * Stricter than `RequirePlatformStaff`: alkera_admin only. Support staff are bounced
 * to the admin home — they reach every other admin surface, just not this one. The
 * server enforces the same gate on the endpoint.
 */
export function RequirePlatformAdmin() {
  const { data, isPending, isError, error, refetch } = useCurrentUser();
  if (isPending) return <GateLoading label="Checking your access" />;
  if (isError) return <GateError error={error} onRetry={() => void refetch()} />;
  if (data?.platform_role !== "alkera_admin") return <Navigate to="/admin" replace />;
  return <Outlet />;
}

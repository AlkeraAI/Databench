import { Navigate, Outlet } from "react-router-dom";

import { useCurrentUser } from "../../api/auth";
import { GateError, GateLoading } from "./GateLoading";

/**
 * Gate the platform-staff surfaces (the Alkera-internal admin console). Any
 * `platform_role` — support or admin — is staff; an account with none is bounced to
 * its dashboard. `RequirePlatformAdmin` is the stricter gate for admin-only pages.
 */
export function RequirePlatformStaff() {
  const { data, isPending, isError, error, refetch } = useCurrentUser();
  if (isPending) return <GateLoading label="Checking your access" />;
  if (isError) return <GateError error={error} onRetry={() => void refetch()} />;
  if (!data?.platform_role) return <Navigate to="/" replace />;
  return <Outlet />;
}

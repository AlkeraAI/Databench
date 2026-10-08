import { Navigate, Outlet } from "react-router-dom";

import { usePublicConfig } from "../../api/config";
import { GateLoading } from "./GateLoading";

/**
 * Gate the self-hosted-only surfaces (the Deployment page). Deployment shape comes
 * from the backend's public config (`self_hosted`), fail-closed to SaaS. On Alkera's
 * hosted app a caller who navigates straight to /org/deployment is bounced to their
 * dashboard: the nav already hides the link (`requiresSelfHosted`) and the backend
 * 404s the deployment-health endpoints — this guard closes the direct-URL gap so the
 * page never renders its broken shell there. The server is the real enforcement.
 *
 * Unlike the role gates, this one has NO failed-read branch, and deliberately: a config read that
 * fails resolves to the shipped default rather than raising, so the guard never sees an error and
 * a deployment shape nobody could confirm is read as the hosted one. Offering a retry plate here
 * would be offering it for a decision that was already made, safely, without asking.
 */
export function RequireSelfHosted() {
  const { data, isPending } = usePublicConfig();
  if (isPending) return <GateLoading label="Checking your access" />;
  if (data?.self_hosted !== true) return <Navigate to="/" replace />;
  return <Outlet />;
}

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { endTabSession } from "../../api/auth";
import { setUnauthorizedHandler } from "../../api/client";

/**
 * Bridges the API client's 401 interceptor to React Query. When any request
 * (other than the `/auth/me` probe) returns 401 mid-session, the interceptor
 * calls our handler, which ends the tab's session and drops the cached identity,
 * so the route guards (`RequireAuth`) re-evaluate as signed-out and redirect to
 * /login. This is the
 * single, app-wide reaction to an expired session; ordinary (non-401) failures
 * stay local to each page. Renders nothing.
 */
export function UnauthorizedBridge() {
  const queryClient = useQueryClient();
  useEffect(() => {
    setUnauthorizedHandler(() => endTabSession({ qc: queryClient, org: "none" }));
    return () => setUnauthorizedHandler(null);
  }, [queryClient]);
  return null;
}

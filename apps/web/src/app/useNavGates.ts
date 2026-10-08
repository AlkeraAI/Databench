// Which nav doorways the signed-in reader can pass: org-admin links for an
// admin, team-admin pages for anyone administering a team, the platform
// console for staff. One owner, so the rail and any in-page link to the same
// page hide together. The route guards still enforce the real boundary.

import { useMemo } from "react";

import { useCurrentUser } from "../api/auth";
import { usePublicConfig } from "../api/config";
import { useIdentityDashboard } from "../api/dashboard";
import type { NavGates } from "./nav";

export function useNavGates(): NavGates {
  const identity = useIdentityDashboard().data;
  const user = useCurrentUser().data;
  // Fail-closed: the Deployment nav stays hidden until public config confirms self-hosted.
  const selfHosted = usePublicConfig().data?.self_hosted === true;
  return useMemo(
    () => ({
      orgAdmin: Boolean(identity?.is_org_admin),
      teamAdmin: Boolean(identity?.is_org_admin) || (user?.admin_team_ids?.length ?? 0) > 0,
      platformStaff: Boolean(user?.platform_role),
      platformAdmin: user?.platform_role === "alkera_admin",
      selfHosted,
    }),
    [identity?.is_org_admin, user?.admin_team_ids, user?.platform_role, selfHosted],
  );
}

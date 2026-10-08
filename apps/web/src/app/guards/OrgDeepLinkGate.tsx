import { useEffect, useRef } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";

import { Button, EmptyState, Skeleton } from "@alkera/ui";

import { useCurrentUser, useMemberships, useSwitchOrg } from "../../api/auth";
import { refusalSentence, stepUpLoginUrl } from "../../api/errors";

/** The query parameter a shared link names its org in (`?org=<uuid>`). */
export const ORG_LINK_PARAM = "org";
/** The query parameter an org's single sign-on returns with, naming the org the
 *  switch was for (`/?switch_org=<uuid>`). */
export const SWITCH_ORG_PARAM = "switch_org";

function without(search: string, name: string): string {
  const params = new URLSearchParams(search);
  params.delete(name);
  const rest = params.toString();
  return rest ? `?${rest}` : "";
}

/**
 * Holds the signed-in shell until a link that names another org has been settled.
 *
 * A shared link carries the org it was made in (`?org=`). Opened while this browser is
 * in another org, the page would load the current org's data under the link's URL, so
 * nothing below renders (and nothing fetches) until the reader decides: switch to the
 * link's org, or stay and go to the overview. An org the reader does not belong to gets
 * one line and nothing else, never its name.
 *
 * It also finishes a switch an org's single sign-on interrupted: the sign-on returns to
 * `?switch_org=<org>`, and the switch completes here (usually the sign-on already moved
 * the session, so the parameter is simply dropped).
 */
export function OrgDeepLinkGate() {
  const user = useCurrentUser().data;
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const linked = params.get(ORG_LINK_PARAM);
  const resumed = params.get(SWITCH_ORG_PARAM);
  const current = user?.org_team_id ?? null;
  const foreignLink = linked !== null && current !== null && linked !== current;
  const pendingSwitch = resumed !== null && current !== null && resumed !== current;
  const memberships = useMemberships({ enabled: foreignLink });
  const switchOrg = useSwitchOrg();
  const resumedOnce = useRef(false);

  useEffect(() => {
    if (resumed === null || current === null) return;
    const rest = `${location.pathname}${without(location.search, SWITCH_ORG_PARAM)}`;
    if (resumed === current) {
      void navigate(rest, { replace: true });
      return;
    }
    if (resumedOnce.current) return;
    resumedOnce.current = true;
    switchOrg.mutate({ orgTeamId: resumed, target: rest });
  }, [resumed, current, location.pathname, location.search, navigate, switchOrg]);

  // Until the session's org is known, a link naming an org cannot be told apart from one
  // into another org: nothing beneath renders yet.
  const undecided = (linked !== null || resumed !== null) && current === null;
  if (pendingSwitch || undecided) {
    return (
      <div className="alk-authgate" role="status" aria-label="Switching organizations">
        <Skeleton width={220} height={14} />
      </div>
    );
  }
  if (!foreignLink) return <Outlet />;
  if (memberships.isPending) {
    return (
      <div className="alk-authgate" role="status" aria-label="Checking the link">
        <Skeleton width={220} height={14} />
      </div>
    );
  }
  const target = memberships.data?.memberships.find((m) => m.org_team_id === linked);
  if (!target) {
    return (
      <div className="alk-authgate">
        <EmptyState title="You don't have access to this." />
      </div>
    );
  }
  const name = target.org_name || "another organization";
  const here = user?.org_name || "this organization";
  return (
    <div className="alk-authgate">
      <EmptyState
        title={`This is in ${name}.`}
        action={
          <div className="alk-orglink__actions">
            <Button
              variant="primary"
              loading={switchOrg.isPending}
              onClick={() =>
                switchOrg.mutate({
                  orgTeamId: target.org_team_id,
                  target: `${location.pathname}${location.search}`,
                })
              }
            >
              {`Switch to ${name}`}
            </Button>
            <Link to="/" replace>
              {`Stay in ${here}`}
            </Link>
          </div>
        }
        details={
          switchOrg.isError && !stepUpLoginUrl(switchOrg.error) ? refusalSentence(switchOrg.error) : undefined
        }
      />
    </div>
  );
}

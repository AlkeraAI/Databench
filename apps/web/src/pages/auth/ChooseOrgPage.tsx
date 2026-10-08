import { useSearchParams } from "react-router-dom";

import { Button, Callout, Skeleton } from "@alkera/ui";

import { GoTo, safeReturnTo } from "./auth-actions";
import { AuthLayout } from "./ui/AuthLayout";
import { useMemberships, useSwitchOrg, type Membership } from "../../api/auth";
import { goTo } from "../../api/activeOrg";
import { refusalSentence, stepUpLoginUrl } from "../../api/errors";
import { usePublicConfig } from "../../api/config";
import { isPendingMembership, useJoinMembership } from "../../api/orgs";
import "./ui/choose-org.css";

/** How a role reads on the chooser. */
const ROLE_LABELS: Record<Membership["role"], string> = { admin: "Admin", member: "Member" };

/**
 * The org chooser (`/choose-org`): where a person in several orgs picks which one this
 * browser enters. Reached after a sign-in whose last-used org needs a step-up first, and
 * after a session whose org membership ended. Each org shows its name and the person's
 * role there; one that takes its single sign-on to enter says so. Picking the org the
 * session is already in just continues; any other goes through the switch, which reloads
 * the app in that org (or starts the org's single sign-on). Where the server runs with
 * several orgs per person, an org that provisioned the person and waits for them reads "Pending" and is joined, never switched into, until the
 * person joins it. A person with one org and nothing pending has nothing to choose and goes
 * straight on.
 */
export function ChooseOrgPage() {
  const [params] = useSearchParams();
  const target = safeReturnTo(params.get("return_to")) ?? "/";
  const memberships = useMemberships();
  const switchOrg = useSwitchOrg();
  const join = useJoinMembership();
  const config = usePublicConfig();
  const multiOrg = config.data?.multi_org_enabled === true;

  // Whether pending orgs count decides whether there is anything to choose, so wait for it.
  if (memberships.isPending || config.isPending) {
    return (
      <AuthLayout title="Choose an organization">
        <div className="pa-choose" role="status" aria-label="Loading your organizations">
          <Skeleton height={44} />
          <Skeleton height={44} />
        </div>
      </AuthLayout>
    );
  }
  if (memberships.isError) {
    return (
      <AuthLayout title="Choose an organization">
        <Callout tone="danger">{refusalSentence(memberships.error)}</Callout>
        <Button variant="secondary" fullWidth onClick={() => void memberships.refetch()}>
          Retry
        </Button>
      </AuthLayout>
    );
  }

  const list = memberships.data.memberships;
  const active = list.filter((m) => !isPendingMembership(m));
  const pending = multiOrg ? list.filter(isPendingMembership) : [];
  if (active.length <= 1 && pending.length === 0) return <GoTo to={target} />;

  const current = memberships.data.active_org_team_id;
  const choose = (m: Membership) => {
    if (m.org_team_id === current) {
      goTo(target);
      return;
    }
    switchOrg.mutate({ orgTeamId: m.org_team_id, target });
  };
  // A 409 with a sign-in URL navigates away on its own; anything else stays here.
  const failed =
    switchOrg.isError && !stepUpLoginUrl(switchOrg.error)
      ? switchOrg.error
      : join.isError && !stepUpLoginUrl(join.error)
        ? join.error
        : null;
  // The org just joined, offered as the next step.
  const joined = join.data && join.data.status === "active" ? join.data : null;
  const busy = switchOrg.isPending || join.isPending;

  return (
    <AuthLayout title="Choose an organization">
      {failed ? <Callout tone="danger">{refusalSentence(failed)}</Callout> : null}
      {joined ? (
        <Callout tone="success" title={`Joined ${joined.org_name || "the organization"}.`}>
          <Button disabled={busy} onClick={() => choose(joined)}>
            {`Switch to ${joined.org_name || "the organization"}`}
          </Button>
        </Callout>
      ) : null}
      <ul className="pa-choose" aria-label="Your organizations">
        {active.map((m) => (
          <li key={m.org_team_id}>
            <button
              type="button"
              className="pa-choose__org"
              onClick={() => choose(m)}
              disabled={busy}
              aria-current={m.org_team_id === current || undefined}
            >
              <span className="pa-choose__name">{m.org_name || "Unnamed organization"}</span>
              <span className="pa-choose__meta">
                {m.sso_required ? "Single sign-on" : ROLE_LABELS[m.role]}
              </span>
            </button>
          </li>
        ))}
        {pending.map((m) => (
          <li key={m.org_team_id} className="pa-choose__pending">
            <span className="pa-choose__name">{m.org_name || "Unnamed organization"}</span>
            <span className="pa-choose__meta">Pending</span>
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              loading={join.isPending && join.variables === m.org_team_id}
              aria-label={`Join ${m.org_name || "Unnamed organization"}`}
              onClick={() => join.mutate(m.org_team_id)}
            >
              Join
            </Button>
          </li>
        ))}
      </ul>
    </AuthLayout>
  );
}

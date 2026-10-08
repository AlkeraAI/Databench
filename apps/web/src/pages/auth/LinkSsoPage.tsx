import { useLocation, useNavigate } from "react-router-dom";

import { Button, Callout, Skeleton } from "@alkera/ui";

import { AuthLayout } from "./ui/AuthLayout";
import { useLogout, useSwitchOrg } from "../../api/auth";
import { ApiError, refusalSentence, stepUpLoginUrl } from "../../api/errors";
import { useCancelSsoLink, useConfirmSsoLink, useSsoLink } from "../../api/orgs";

/** What the page says when no link request is live for this browser. */
export const SSO_LINK_EXPIRED = "This link has expired. Sign in with single sign-on again.";

const TITLE = "Link single sign-on";

/**
 * Where an org's single sign-on sends a person whose email already has an account but no
 * membership in that org (`/link-sso?org=<id>`). The person has signed in as that existing
 * account first (the route sits behind sign-in and returns here), so linking is their own
 * decision, made with a credential they already hold: "Link" attaches the org's sign-on
 * identity to the account, and joins the org when it asked for them; "Cancel" discards it.
 */
export function LinkSsoPage() {
  const link = useSsoLink();
  const confirm = useConfirmSsoLink();
  const cancel = useCancelSsoLink();
  const switchOrg = useSwitchOrg();
  const logout = useLogout();
  const navigate = useNavigate();
  const location = useLocation();
  const here = `${location.pathname}${location.search}`;

  if (link.isPending) {
    return (
      <AuthLayout title={TITLE}>
        <div role="status" aria-label="Loading the link">
          <Skeleton height={44} />
        </div>
      </AuthLayout>
    );
  }

  if (link.isError) {
    const error = link.error instanceof ApiError ? link.error : null;
    if (error?.code === "sso_link_other_account") {
      return (
        <AuthLayout title={TITLE}>
          <Callout tone="warning">{refusalSentence(error)}</Callout>
          <Button
            fullWidth
            loading={logout.isPending}
            onClick={() =>
              logout.mutate(undefined, {
                onSettled: () => navigate(`/login?return_to=${encodeURIComponent(here)}`, { replace: true }),
              })
            }
          >
            Sign out
          </Button>
        </AuthLayout>
      );
    }
    const expired = !error || error.status === 404;
    return (
      <AuthLayout title={TITLE}>
        <Callout tone={expired ? "warning" : "danger"}>{expired ? SSO_LINK_EXPIRED : refusalSentence(error)}</Callout>
      </AuthLayout>
    );
  }

  const request = link.data;
  const done = confirm.data;
  const orgName = request.org_name || "the organization";

  if (done) {
    const switchFailed = switchOrg.isError && !stepUpLoginUrl(switchOrg.error) ? switchOrg.error : null;
    return (
      <AuthLayout title={TITLE}>
        {done.message ? <Callout tone={done.joined ? "success" : "info"}>{done.message}</Callout> : null}
        {switchFailed ? <Callout tone="danger">{switchFailed.message}</Callout> : null}
        {done.joined ? (
          <Button
            fullWidth
            loading={switchOrg.isPending}
            onClick={() => switchOrg.mutate({ orgTeamId: done.org_team_id })}
          >
            {`Switch to ${orgName}`}
          </Button>
        ) : (
          <Button fullWidth variant="secondary" onClick={() => navigate("/", { replace: true })}>
            Continue
          </Button>
        )}
      </AuthLayout>
    );
  }

  const refused = confirm.error instanceof ApiError ? confirm.error : null;
  const refusedText = refused ? (refused.status === 404 ? SSO_LINK_EXPIRED : refused.message) : null;

  return (
    <AuthLayout title={`Link ${request.email_masked} to ${orgName}'s single sign-on?`}>
      {refusedText ? <Callout tone="danger">{refusedText}</Callout> : null}
      <Button fullWidth loading={confirm.isPending} disabled={cancel.isPending} onClick={() => confirm.mutate()}>
        Link
      </Button>
      <Button
        fullWidth
        variant="secondary"
        loading={cancel.isPending}
        disabled={confirm.isPending}
        onClick={() => cancel.mutate(undefined, { onSettled: () => navigate("/", { replace: true }) })}
      >
        Cancel
      </Button>
    </AuthLayout>
  );
}

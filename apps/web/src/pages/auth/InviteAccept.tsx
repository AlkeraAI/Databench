import { useNavigate } from "react-router-dom";

import { Button, Callout } from "@alkera/ui";

import { AuthLayout } from "./ui/AuthLayout";
import { safeReturnTo } from "./auth-actions";
import { useLogout, useSwitchOrg, type CurrentUser, type InvitationPreview } from "../../api/auth";
import { ApiError, refusalSentence, stepUpLoginUrl } from "../../api/errors";
import { usePublicConfig } from "../../api/config";
import { useAcceptInvitationByToken } from "../../api/orgs";

/** Where an invited reader who already has an account finds the invitation once signed in. */
export const INVITATIONS_PATH = "/teams?tab=invites";

/** The code a registration answers, with several orgs per person, for an address that
 *  already has an account. */
export const ACCOUNT_EXISTS_CODE = "account_exists";

/** Sign-in carrying an invitation, so the sign-in comes back to accept it. */
export function loginWithInvite(token: string): string {
  return `/login?invite=${encodeURIComponent(token)}`;
}

/** Where an invitation lands. An invitation to the org root names one place, not
 *  the same name twice, as does one whose team happens to share the org's name. */
export function InviteDestination({ preview }: { preview: InvitationPreview }) {
  if (preview.team_id === preview.org_team_id || preview.team_name === preview.org_name) {
    return <strong>{preview.org_name}</strong>;
  }
  return (
    <>
      <strong>{preview.team_name}</strong> in <strong>{preview.org_name}</strong>
    </>
  );
}

/**
 * A registration refused because the address already has an account: the server's
 * sentence, and a way to sign in that keeps the invitation (the server names the
 * sign-in path in `details.next`; only a sign-in path in this app is followed).
 */
export function AccountExistsNotice({
  message,
  details,
  inviteToken,
}: {
  message: string;
  details: Readonly<Record<string, unknown>> | null;
  inviteToken?: string;
}) {
  const navigate = useNavigate();
  const next = typeof details?.next === "string" ? safeReturnTo(details.next) : null;
  const fallback = inviteToken ? loginWithInvite(inviteToken) : "/login";
  const target = next && (next === "/login" || next.startsWith("/login?")) ? next : fallback;
  return (
    <Callout tone="info" title={message}>
      <Button onClick={() => navigate(target)}>Sign in</Button>
    </Callout>
  );
}

/**
 * The invitation link opened by someone already signed in. The address the invitation names
 * decides the answer: theirs → accept it, another → sign out to use it. Where the server runs
 * with several orgs per person it is accepted here, then the switch into the org it joined is
 * offered; elsewhere the reader opens their invitations list, where it is accepted or its
 * refusal explained.
 */
export function SignedInInvite({
  me,
  preview,
  token,
}: {
  me: CurrentUser;
  preview: InvitationPreview | undefined;
  token: string;
}) {
  const logout = useLogout();
  const navigate = useNavigate();
  const accept = useAcceptInvitationByToken();
  const switchOrg = useSwitchOrg();
  const forThem = !preview || preview.email.toLowerCase() === me.email.toLowerCase();
  const config = usePublicConfig();
  const multiOrg = config.data?.multi_org_enabled === true;
  const error = accept.error instanceof ApiError ? accept.error : null;

  const signOutForInvite = () =>
    logout.mutate(undefined, { onSettled: () => navigate(loginWithInvite(token), { replace: true }) });

  const joined = accept.data;
  const joinedOrg = joined?.org_team_id && joined.org_team_id !== me.org_team_id ? joined.org_team_id : null;
  const joinedName = joined?.org_name || preview?.org_name || "the organization";
  const switchFailed = switchOrg.isError && !stepUpLoginUrl(switchOrg.error) ? switchOrg.error : null;

  let body;
  if (!forThem) {
    body = (
      <>
        <Callout tone="warning">This invitation is for {preview?.email}. Sign out to use it.</Callout>
        <Button fullWidth loading={logout.isPending} onClick={() => logout.mutate()}>
          Sign out
        </Button>
      </>
    );
  } else if (config.isPending) {
    // Which way it is accepted is not known yet.
    body = null;
  } else if (!multiOrg) {
    body = (
      <Button fullWidth onClick={() => navigate(INVITATIONS_PATH)}>
        Open your invitations
      </Button>
    );
  } else if (joined) {
    body = (
      <>
        <Callout tone="success">{`Joined ${joinedName}.`}</Callout>
        {switchFailed ? <Callout tone="danger">{refusalSentence(switchFailed)}</Callout> : null}
        {joinedOrg ? (
          <Button
            fullWidth
            loading={switchOrg.isPending}
            onClick={() => switchOrg.mutate({ orgTeamId: joinedOrg })}
          >
            {`Switch to ${joinedName}`}
          </Button>
        ) : (
          <Button fullWidth onClick={() => navigate("/")}>
            Continue
          </Button>
        )}
      </>
    );
  } else if (error?.code === "invitation_other_account") {
    body = (
      <>
        <Callout tone="warning">{refusalSentence(error)}</Callout>
        <Button fullWidth loading={logout.isPending} onClick={signOutForInvite}>
          Sign out
        </Button>
      </>
    );
  } else if (error?.code === "email_verification_required") {
    body = (
      <>
        <Callout tone="warning">{refusalSentence(error)}</Callout>
        <Button fullWidth onClick={() => navigate("/verify-email")}>
          Verify email
        </Button>
      </>
    );
  } else {
    body = (
      <>
        {accept.isError ? <Callout tone="danger">{refusalSentence(accept.error)}</Callout> : null}
        <Button fullWidth loading={accept.isPending} onClick={() => accept.mutate(token)}>
          Accept invitation
        </Button>
      </>
    );
  }

  return (
    <AuthLayout title="Accept your invitation" lede={`You're signed in as ${me.email}.`}>
      {preview && !joined ? (
        <Callout tone="info">
          Joining <InviteDestination preview={preview} />.
        </Callout>
      ) : null}
      {body}
    </AuthLayout>
  );
}

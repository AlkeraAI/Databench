import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Callout, Button, TextInput } from "@alkera/ui";

import { AuthLayout } from "./ui/AuthLayout";
import { CaptchaWidget } from "./ui/CaptchaWidget";
import { captchaEnabled, useCaptcha } from "./ui/captcha";
import { FormError } from "./ui/FormError";
import { OAuthOptions } from "./ui/OAuthOptions";
import { useCurrentUser, useInvitationByToken, useOAuthRegisterContext } from "../../api/auth";
import { ApiError, refusalSentence } from "../../api/errors";
import {
  ACCOUNT_EXISTS_CODE,
  AccountExistsNotice,
  INVITATIONS_PATH,
  InviteDestination,
  SignedInInvite,
  loginWithInvite,
} from "./InviteAccept";
import { useMultiOrgEnabled } from "../../api/orgs";
import { postAuthDestination, useAuthActions, withCarriedParams } from "./auth-actions";
import { useAuthForm } from "./useAuthForm";
import { PASSWORD_MIN, firstError, isDisplayName, isEmail, isPassword, required } from "../../lib/validation";

/**
 * Minimal signup: just an email and a password. Name and organization
 * are collected on the next step (/complete-profile) — the new account lands there
 * via the profile gate, so signup itself stays short.
 *
 * Query params it honors:
 *   `?invite=<token>`      — join the inviter's org (a preview of the team renders).
 *   an extension's carried parameter — kept on the sign-in cross-link, and turned into
 *                            the post-signup landing (CARRIED_AUTH_PARAMS).
 *   `?oauth_ticket=<t>`    — OAuth-register mode (the backend callback lands here
 *                            for a brand-new provider account): confirm the name and
 *                            org, no password or captcha.
 *   `?allow_personal=1`    — the explicit personal-email opt-in, set by the OAuth
 *                            business-email page's "continue anyway".
 *   `?return_to=<path>`    — carried back by the OAuth callback; honored (sanitized)
 *                            by the post-signup navigation in the auth-actions seam.
 */
export function SignupPage() {
  const [params] = useSearchParams();
  const oauthTicket = params.get("oauth_ticket");
  const inviteToken = params.get("invite") ?? undefined;
  const allowPersonal = params.get("allow_personal") === "1";
  // Where this signup will land (deep link / carried landing / home), for threading
  // through the OAuth handshake — the provider round-trip drops our query string,
  // so the backend carries it as `return_to` and hands it back on the callback.
  const dest = postAuthDestination(params.toString());

  if (oauthTicket) {
    return <OAuthSignupForm ticket={oauthTicket} allowPersonal={allowPersonal} />;
  }
  return (
    <PasswordSignupForm
      inviteToken={inviteToken}
      allowPersonal={allowPersonal}
      oauthReturnTo={dest === "/" ? undefined : dest}
    />
  );
}

export { INVITATIONS_PATH };

/** The /login link: keeps the carried parameters so a returning reader still lands where
 *  they were headed. An invitation's reader carries the invitation through sign-in and comes back to accept it where
 *  the server runs with several orgs per person, and otherwise lands on their invitations, where
 *  it can be accepted (or, when their account cannot take it, where it says why). */
function signInHref(params: URLSearchParams, inviteToken: string | undefined, multiOrg: boolean): string {
  if (inviteToken) {
    return multiOrg ? loginWithInvite(inviteToken) : `/login?return_to=${encodeURIComponent(INVITATIONS_PATH)}`;
  }
  return withCarriedParams("/login", params);
}

function SignInFooter({ inviteToken }: { inviteToken?: string }) {
  const multiOrg = useMultiOrgEnabled();
  const [params] = useSearchParams();
  return (
    <>
      Already have an account?{" "}
      <Link className="alk-link" to={signInHref(params, inviteToken, multiOrg)}>
        Sign in
      </Link>
    </>
  );
}

/** What an invited reader is asked to do. "Organization", the noun the
 *  invitation email uses, so the two never name the destination differently. */
export const INVITE_LEDE = "Set a password to join your organization.";

function PasswordSignupForm({
  inviteToken,
  allowPersonal,
  oauthReturnTo,
}: {
  inviteToken?: string;
  allowPersonal: boolean;
  oauthReturnTo?: string;
}) {
  const actions = useAuthActions();
  const { submit, showErrors, handleSubmit, formRef } = useAuthForm();
  const captcha = useCaptcha();
  const hasInvite = Boolean(inviteToken);
  const invite = useInvitationByToken(inviteToken);
  const inviteError = hasInvite && invite.isError;
  const preview = invite.data;
  // A link that was used, declined, withdrawn or has lapsed says which (410 with the reason); any
  // other failure is a link that never was.
  const closedReason = invite.error instanceof ApiError && invite.error.status === 410 ? refusalSentence(invite.error, { fallback: "" }) || null : null;
  const me = useCurrentUser().data ?? null;

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  // Signup against a token only succeeds for the address the invitation names,
  // so once the preview answers, that address IS the field — the invitee has
  // nothing to type and nothing to get wrong.
  const invitedEmail = preview?.email;
  const emailValue = invitedEmail ?? email;

  // A failed signup consumes the single-use captcha token; fetch a fresh one.
  const { reset: resetCaptcha } = captcha;
  useEffect(() => {
    if (submit.error) resetCaptcha();
  }, [submit.error, resetCaptcha]);

  const errors = {
    email: isEmail()(emailValue),
    password: isPassword()(password),
  };
  const shown = showErrors ? errors : null;
  const hasError = Boolean(firstError(...Object.values(errors)));
  const captchaBlocked = captchaEnabled() && !captcha.token;

  const signupWith = (allowPersonalEmail: boolean) =>
    actions.signup({
      email: emailValue,
      password,
      inviteToken,
      allowPersonalEmail,
      turnstileToken: captcha.token,
    });

  // The backend's business-email gate refused the address; surface the explicit
  // opt-in (the resubmit waits for the refreshed captcha token, like any retry).
  const personalBlocked = submit.errorCode === "personal_email_blocked";

  // A live invitation's reader who signs in comes back to accept it; a closed link's lands home.
  const footer = <SignInFooter inviteToken={hasInvite && !inviteError ? inviteToken : undefined} />;
  // The address already has an account: sign in instead, keeping the invitation.
  const accountExists = submit.errorCode === ACCOUNT_EXISTS_CODE;

  // Signed in already: there is no account to create. The invitation is accepted here.
  if (inviteToken && me && !inviteError) {
    return <SignedInInvite me={me} preview={preview} token={inviteToken} />;
  }

  if (submit.success) {
    return (
      <AuthLayout title="Account created" lede="Next, finish setting up your profile." footer={footer}>
        <Callout tone="success" title="You're in">
          In the product you'd continue to add your name and organization now.
        </Callout>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title={hasInvite ? "Accept your invitation" : "Create your account"}
      lede={hasInvite ? INVITE_LEDE : "Just an email and a password to start."}
      footer={footer}
    >
      <form
        ref={formRef}
        className="pa-auth__form"
        noValidate
        onSubmit={handleSubmit(hasError || inviteError, () => signupWith(allowPersonal))}
      >
        {hasInvite ? (
          inviteError ? (
            <Callout tone="danger" title="Invitation problem">
              {closedReason ?? "This invitation is invalid or expired. Ask your organization for a new link."}
            </Callout>
          ) : (
            // The lede already says what to do here; the callout says only
            // where the reader is going, which the lede cannot.
            <Callout tone="info">
              {preview ? (
                <>
                  Joining <InviteDestination preview={preview} />. You&apos;ll add your name next.
                </>
              ) : (
                "You'll add your name next."
              )}
            </Callout>
          )
        ) : null}
        {inviteError ? null : (
          <>
        {accountExists && submit.error ? (
          <AccountExistsNotice message={submit.error} details={submit.errorDetails} inviteToken={inviteToken} />
        ) : (
          <FormError title="Couldn't create your account" error={submit.error} />
        )}
        {personalBlocked ? (
          <button
            type="button"
            className="alk-link"
            disabled={captchaBlocked}
            onClick={() => void submit.run(() => signupWith(true))}
          >
            Continue with this email anyway
          </button>
        ) : null}
        <TextInput
          label="Email"
          type="email"
          autoComplete="email"
          placeholder="you@company.com"
          description={
            invitedEmail
              ? "The invitation only works for this address."
              : hasInvite
                ? undefined
                : "Use your work email so teammates can find your org."
          }
          required
          disabled={Boolean(invitedEmail)}
          value={emailValue}
          error={shown?.email}
          onChange={(event) => setEmail(event.target.value)}
        />
        <TextInput type="password"
          label="Password"
          autoComplete="new-password"
          placeholder="Create a password"
          description={`At least ${PASSWORD_MIN} characters.`}
          required
          value={password}
          error={shown?.password}
          onChange={(event) => setPassword(event.target.value)}
        />
        <CaptchaWidget ref={captcha.ref} onToken={captcha.setToken} />
        <Button type="submit" fullWidth loading={submit.pending} disabled={captchaBlocked || inviteError}>
          {hasInvite ? "Accept and create account" : "Create account"}
        </Button>
          </>
        )}
      </form>
      {inviteError ? null : <OAuthOptions intent="up" inviteToken={inviteToken} returnTo={oauthReturnTo} />}
    </AuthLayout>
  );
}

/**
 * OAuth-register mode (`?oauth_ticket=`): the provider already verified the email,
 * so there's no password or captcha — just confirm the name (prefilled from the
 * provider) and, without an invite, name the org. The backend requires the name
 * here, so this path skips the complete-profile hop entirely.
 */
function OAuthSignupForm({
  ticket,
  allowPersonal,
}: {
  ticket: string;
  allowPersonal: boolean;
}) {
  const actions = useAuthActions();
  const { submit, showErrors, handleSubmit, formRef } = useAuthForm();
  const context = useOAuthRegisterContext(ticket);
  const ctx = context.data;
  const hasInvite = Boolean(ctx?.has_invite);
  const needsOrg = !hasInvite;

  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [org, setOrg] = useState("");

  // Prefill from the ticket once its context loads — exactly once, and never
  // clobbering anything the user already typed.
  const prefilled = useRef(false);
  useEffect(() => {
    if (!ctx || prefilled.current) return;
    prefilled.current = true;
    if (ctx.first_name) setFirstName((value) => value || ctx.first_name);
    if (ctx.last_name) setLastName((value) => value || ctx.last_name);
    if (!ctx.has_invite && ctx.first_name) {
      setOrg((value) => value || `${ctx.first_name}'s Organization`);
    }
  }, [ctx]);

  const footer = <SignInFooter />;

  if (context.isError) {
    return (
      <AuthLayout
        title="Sign-up link expired"
        lede="This link is invalid or has expired."
        footer={
          <Link className="alk-link" to="/login">
            Back to sign in
          </Link>
        }
      >
        <Callout tone="danger" title="Start again">
          Head back to the sign-in page and try your provider again.
        </Callout>
      </AuthLayout>
    );
  }

  // Every one of these lands in an invitation email this account later sends, so
  // each carries the display-name policy alongside its presence check.
  const errors = {
    firstName: firstError(required("Enter your first name")(firstName), isDisplayName()(firstName)),
    lastName: firstError(required("Enter your last name")(lastName), isDisplayName()(lastName)),
    org: needsOrg
      ? firstError(required("Name your organization")(org), isDisplayName()(org))
      : null,
  };
  const shown = showErrors ? errors : null;
  const hasError = Boolean(firstError(...Object.values(errors)));

  const provider = ctx ? ctx.provider.charAt(0).toUpperCase() + ctx.provider.slice(1) : "your provider";

  if (submit.success) {
    return (
      <AuthLayout title="Account created" lede="Taking you to your workspace…" footer={footer}>
        <Callout tone="success" title="You're in">
          Your account is ready. In the product you'd land on your workspace now.
        </Callout>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title={hasInvite ? "Accept your invitation" : "Finish signing up"}
      lede={`Confirm your details from ${provider} to create your account.`}
      footer={footer}
    >
      <form
        ref={formRef}
        className="pa-auth__form"
        noValidate
        onSubmit={handleSubmit(hasError, () =>
          actions.oauthRegister({
            ticket,
            firstName,
            lastName,
            orgName: needsOrg ? org : undefined,
            allowPersonalEmail: allowPersonal,
          }),
        )}
      >
        {submit.errorCode === ACCOUNT_EXISTS_CODE && submit.error ? (
          <AccountExistsNotice message={submit.error} details={submit.errorDetails} />
        ) : (
          <FormError title="Couldn't create your account" error={submit.error} />
        )}
        <TextInput
          label="Email"
          type="email"
          description={`Verified by ${provider}.`}
          disabled
          value={ctx?.email ?? ""}
        />
        <div className="pa-auth__names">
          <TextInput
            label="First name"
            autoComplete="given-name"
            required
            value={firstName}
            error={shown?.firstName}
            onChange={(event) => setFirstName(event.target.value)}
          />
          <TextInput
            label="Last name"
            autoComplete="family-name"
            required
            value={lastName}
            error={shown?.lastName}
            onChange={(event) => setLastName(event.target.value)}
          />
        </div>
        {needsOrg ? (
          <TextInput
            label="Organization name"
            autoComplete="organization"
            required
            value={org}
            error={shown?.org}
            onChange={(event) => setOrg(event.target.value)}
          />
        ) : null}
        <Button type="submit" fullWidth loading={submit.pending} disabled={!ctx}>
          {hasInvite ? "Accept and create account" : "Create account"}
        </Button>
      </form>
    </AuthLayout>
  );
}

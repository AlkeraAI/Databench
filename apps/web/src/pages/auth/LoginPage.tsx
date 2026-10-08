import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Button, Callout, currentBrand, Skeleton, TextInput } from "@alkera/ui";

import { useCurrentUser, useInvitationByToken } from "../../api/auth";
import { SignedInInvite } from "./InviteAccept";
import { AuthLayout } from "./ui/AuthLayout";
import { CaptchaWidget } from "./ui/CaptchaWidget";
import { captchaEnabled, useCaptcha } from "./ui/captcha";
import { FormError } from "./ui/FormError";
import { OAuthOptions } from "./ui/OAuthOptions";
import { invitationPath, postAuthDestination, useAuthActions, withCarriedParams } from "./auth-actions";
import { useAuthForm } from "./useAuthForm";
import { firstError, isEmail, required } from "../../lib/validation";

/** What a refused federated sign-in means, and what the user can DO about it.
 *
 * The OAuth and SSO callbacks can only answer a browser with a redirect, so every
 * refusal lands back here as `/login?oauth_error=<reason>` — a short, stable code
 * that never reveals whether an account exists. Rendering nothing (what this page
 * did before) made every one of them indistinguishable from a dead button: the
 * user was returned to a pristine login form with no hint that a server-side gate
 * had refused them, or which of the other ways in still works. So each reason
 * names the NEXT STEP rather than the code, and anything unrecognized falls back
 * to the generic notice — the raw parameter is never echoed, since anyone can put
 * any string in it.
 *
 * Keyed to the reasons `oauth.py`'s `_login_error_redirect` and `sso.py`'s
 * `_login_error` actually emit (including every `OAuthLoginBlockedError.reason`
 * those two funnel through).
 */
const OAUTH_ERROR_NOTICES: Record<string, { title: string; body: string }> = {
  sso_required: {
    title: "Your organization requires single sign-on",
    body: "Sign in through your organization's identity provider instead of a social account. Your admin can send you the link.",
  },
  sso_sign_in_required: {
    title: "Sign in through your organization",
    body: "Your organization set up this account, so it opens through its identity provider. Use your organization's sign-in instead of a social account.",
  },
  provider_disabled: {
    title: "That provider isn't allowed here",
    body: "Your organization has turned that sign-in option off. Use your email and password below, or another provider your admin allows.",
  },
  email_unverified: {
    title: "That provider hasn't verified this address",
    body: "A sign-in provider is used only for an address it has verified. Verify it with the provider, or sign in with your email and password below.",
  },
  already_linked: {
    title: "That account is already linked",
    body: "That provider account belongs to a different user. Sign in with the account you linked it to, or use your email and password below.",
  },
  account_deactivated: {
    title: "This account is deactivated",
    body: "Ask your organization's admin to restore it, then sign in again.",
  },
  no_email: {
    title: "That provider didn't share an email address",
    body: "Allow email access in the provider's settings and try again, or sign in with your email and password below.",
  },
  expired: {
    title: "That sign-in took too long",
    body: "It expired before the provider sent you back. Start it again from the button below.",
  },
  state: {
    title: "We couldn't verify that sign-in",
    body: "What came back didn't match the sign-in we started. Start again from the button below, and only follow sign-in links you opened yourself.",
  },
  exchange: {
    title: "The provider couldn't complete the sign-in",
    body: "Try again in a moment, or sign in with your email and password below.",
  },
  provider_unavailable: {
    title: "That provider isn't reachable right now",
    body: "Try again shortly, or sign in with your email and password below.",
  },
  unknown_provider: {
    title: "That sign-in option isn't available",
    body: "Use your email and password below, or pick one of the providers shown.",
  },
  sso_domain_mismatch: {
    title: "That address isn't covered by this sign-on",
    body: "Your organization's identity provider signs in its own email domains only. Use your work address, or sign in with your email and password below.",
  },
  sso_org_mismatch: {
    title: "That account belongs to another organization",
    body: "Sign in through the organization that owns the account, or use your email and password below.",
  },
  sso_not_configured: {
    title: "Single sign-on isn't set up",
    body: "That organization has no identity provider configured. Ask your admin, or sign in with your email and password below.",
  },
  sso_misconfigured: {
    title: "Single sign-on isn't configured correctly",
    body: "Ask your organization's admin to check the connection, then try again.",
  },
  replay: {
    title: "That sign-in link was already used",
    body: "Start again from your organization's sign-in page.",
  },
};

/** The signup cross-link: keeps an invitation being carried, else the carried parameters. */
function signupHref(params: URLSearchParams): string {
  const invite = params.get("invite");
  if (invite) return invitationPath(invite);
  return withCarriedParams("/signup", params);
}

const GENERIC_OAUTH_NOTICE = {
  title: "Couldn't finish that sign-in",
  body: "Start it again from the button below, or sign in with your email and password.",
};

/**
 * Sign-in. A sign-in carrying an invitation (`?invite=<token>`, where an invitation link
 * sends a person who already has an account) comes back to that invitation afterwards;
 * opened by someone already signed in, it offers to accept the invitation at once.
 */
export function LoginPage() {
  const [params] = useSearchParams();
  const invite = params.get("invite");
  return invite ? <InviteLogin token={invite} /> : <LoginForm />;
}

function InviteLogin({ token }: { token: string }) {
  const me = useCurrentUser();
  const preview = useInvitationByToken(token);
  if (me.isPending) {
    return (
      <AuthLayout title="Sign in">
        <Skeleton height={44} />
      </AuthLayout>
    );
  }
  if (me.data && !preview.isError) {
    return <SignedInInvite me={me.data} preview={preview.data} token={token} />;
  }
  return <LoginForm />;
}

function LoginForm() {
  const actions = useAuthActions();
  const { submit, showErrors, handleSubmit, formRef } = useAuthForm();
  const captcha = useCaptcha();
  const [params] = useSearchParams();
  // The post-login landing spot (threaded through the OAuth handshake as `return_to` —
  // the provider round-trip drops our query string). The email/password path's landing
  // is owned by the auth-actions seam, which reads the same params.
  const dest = postAuthDestination(params.toString());
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaCode, setMfaCode] = useState("");

  // An MFA-protected account 401s the first attempt with `mfa_required`; a wrong
  // code answers `mfa_invalid`. Either reveals the code field so the user can
  // enter it and resubmit email + password + code.
  const mfaRequired = submit.errorCode === "mfa_required" || submit.errorCode === "mfa_invalid";

  // Why the provider round-trip bounced back here. A fresh email/password attempt
  // supersedes it — that error is about what the user just did, the notice about a
  // trip they've already left.
  const oauthError = params.get("oauth_error");
  const oauthNotice =
    oauthError && !submit.error ? (OAUTH_ERROR_NOTICES[oauthError] ?? GENERIC_OAUTH_NOTICE) : null;

  // A failed login consumes the single-use captcha token; fetch a fresh one so the
  // retry isn't rejected as a replay.
  const { reset: resetCaptcha } = captcha;
  useEffect(() => {
    if (submit.error) resetCaptcha();
  }, [submit.error, resetCaptcha]);

  const errors = {
    email: isEmail()(email),
    password: required("Enter your password")(password),
    // The code field only validates once it's shown — a first attempt has no code.
    mfaCode: mfaRequired ? required("Enter your authentication code")(mfaCode) : null,
  };
  const shown = showErrors ? errors : null;
  const hasError = Boolean(firstError(...Object.values(errors)));
  const captchaBlocked = captchaEnabled() && !captcha.token;

  const footer = (
    <>
      New to {currentBrand().productName}?{" "}
      <Link className="alk-link" to={signupHref(params)}>
        Create an account
      </Link>
    </>
  );

  if (submit.success) {
    return (
      <AuthLayout title="You're signed in" lede="Taking you to your workspace…">
        <Callout tone="success" title="Signed in">
          Your session is ready. In the product you'd land on your workspace now.
        </Callout>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="Sign in"
      footer={footer}
    >
      {oauthNotice ? (
        <Callout tone="warning" title={oauthNotice.title}>
          {oauthNotice.body}
        </Callout>
      ) : null}
      <form
        ref={formRef}
        className="pa-auth__form"
        noValidate
        onSubmit={handleSubmit(hasError, () =>
          actions.login({ email, password, mfaCode: mfaRequired ? mfaCode : undefined, turnstileToken: captcha.token }),
        )}
      >
        {/* When the account is MFA-protected, the password line is right — surface the
            "enter your code" prompt, not the raw error, so it reads as a next step. */}
        <FormError
          title={mfaRequired ? "One more step" : "Couldn't sign you in"}
          error={mfaRequired ? "Enter the code from your authenticator app to finish signing in." : submit.error}
        />
        <TextInput
          label="Email"
          type="email"
          autoComplete="email"
          placeholder="you@company.com"
          required
          requiredMark={false}
          value={email}
          error={shown?.email}
          onChange={(event) => setEmail(event.target.value)}
        />
        <div style={{ position: "relative" }}>
          <Link className="alk-link pa-auth__forgot" to="/forgot-password">
            Forgot password?
          </Link>
          <TextInput type="password"
            label="Password"
            autoComplete="current-password"
            placeholder="Your password"
            required
            requiredMark={false}
            value={password}
            error={shown?.password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
        {mfaRequired ? (
          <TextInput
            label="Authentication code"
            placeholder="6-digit code or backup code"
            autoComplete="one-time-code"
            inputMode="numeric"
            autoFocus
            required
            value={mfaCode}
            error={submit.errorCode === "mfa_invalid" ? "That code didn't match. Try again." : shown?.mfaCode}
            onChange={(event) => setMfaCode(event.target.value)}
          />
        ) : null}
        <CaptchaWidget ref={captcha.ref} onToken={captcha.setToken} />
        <Button type="submit" fullWidth loading={submit.pending} disabled={captchaBlocked}>
          Sign in
        </Button>
      </form>
      <OAuthOptions intent="in" returnTo={dest === "/" ? undefined : dest} />
    </AuthLayout>
  );
}

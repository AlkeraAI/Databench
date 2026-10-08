import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { Button, Callout, currentBrand } from "@alkera/ui";

import { AuthLayout } from "./ui/AuthLayout";
import { oauthStartUrl, useOAuthRegisterContext } from "../../api/auth";

/**
 * Shown when a brand-new user finishes OAuth with a personal email (the backend
 * callback redirects here instead of /signup). We nudge them to redo sign-in with
 * a work account — for Google the handshake forces the account chooser. A small,
 * de-emphasized link lets a genuine personal user continue anyway, forwarding to
 * the normal signup with `allow_personal=1`.
 */
export function OAuthBusinessEmailPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const ticket = params.get("oauth_ticket") ?? undefined;
  const returnTo = params.get("return_to") ?? "/";

  const context = useOAuthRegisterContext(ticket);
  const ctx = context.data;

  if (!ticket || context.isError) {
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

  const continueAnyway = () => {
    const next = new URLSearchParams({ oauth_ticket: ticket, return_to: returnTo, allow_personal: "1" });
    navigate(`/signup?${next.toString()}`);
  };

  const provider = ctx?.provider ? ctx.provider.charAt(0).toUpperCase() + ctx.provider.slice(1) : "your provider";

  return (
    <AuthLayout
      title="Use your work account"
      lede={`Sign in with your work email to set ${currentBrand().productName} up for your team.`}
      footer={
        <button type="button" className="alk-link" onClick={continueAnyway}>
          {ctx?.email ? `Continue with ${ctx.email} anyway` : "Continue with this account anyway"}
        </button>
      }
    >
      <Callout tone="info" title={`That's a personal ${provider} account`}>
        {ctx?.email ? <strong>{ctx.email}</strong> : "This account"} looks personal. Signing in with your work
        email keeps your data with your organization and makes it easier for teammates to find you.
      </Callout>
      <Button
        type="button"
        fullWidth
        disabled={!ctx}
        onClick={() => {
          if (!ctx) return;
          window.location.href = oauthStartUrl(ctx.provider, { intent: "signup", returnTo });
        }}
      >
        Sign in with your work account
      </Button>
    </AuthLayout>
  );
}

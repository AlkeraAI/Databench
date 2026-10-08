import { Navigate, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { Button, Callout, currentBrand, Skeleton } from "@alkera/ui";

import { AuthLayout } from "./ui/AuthLayout";
import {
  isVerificationBlocked,
  meKey,
  useCurrentUser,
  useLogout,
  useResendVerification,
} from "../../api/auth";
import { usePublicConfig } from "../../api/config";
import { refusalSentence } from "../../api/errors";
import { RESEND_REFUSAL } from "./resendRefusal";

/**
 * Full-screen gate for an account whose email-verification grace window has lapsed.
 * {@link RequireAuth} redirects blocked-past-grace accounts here, and the API + gateway
 * refuse them in parallel — so this is the only product surface they can reach until they
 * confirm their email. Verified / still-in-grace users that land here are bounced to the app.
 */
export function EmailVerificationGatePage() {
  const { data: user, isPending } = useCurrentUser();
  const resend = useResendVerification();
  // The deployment's support address: null when it configured none, undefined while the
  // config is still loading (the footer then names neither).
  const config = usePublicConfig();
  const supportEmail = config.data ? config.data.support_email : undefined;
  const logout = useLogout();
  const navigate = useNavigate();
  const qc = useQueryClient();
  // Re-check verification by refetching the identity (the hooks own that cache) —
  // not a full document reload, which would throw away all React Query state and
  // the warm bundle. Once verified, this page's own gate bounces the user to "/".
  const recheck = () => void qc.invalidateQueries({ queryKey: meKey });

  if (isPending) {
    return (
      <AuthLayout title="One moment" lede="Loading your account.">
        <div className="pa-auth__success" role="status" aria-label="Loading your account">
          <Skeleton width={200} height={14} />
        </div>
      </AuthLayout>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  if (!isVerificationBlocked(user)) return <Navigate to="/" replace />;

  const status: "idle" | "sent" | "error" = resend.isSuccess
    ? "sent"
    : resend.isError
      ? "error"
      : "idle";

  const footer = (
    <>
      Already verified?{" "}
      <button type="button" className="alk-link" onClick={recheck}>
        Reload
      </button>
      {supportEmail ? (
        <>
          {" "}
          · Having trouble?{" "}
          <a className="alk-link" href={`mailto:${supportEmail}`}>
            Contact support
          </a>
        </>
      ) : null}
      {supportEmail === null ? " · Having trouble? Ask your administrator." : null}
    </>
  );

  return (
    <AuthLayout
      title="Verify your email to continue"
      lede={
        <>
          To keep using {currentBrand().productName}, confirm your email address.{" "}
          {user.verification_resend_available_at != null ? (
            <>
              We sent a verification link to <strong>{user.email}</strong>. Check your spam folder if
              it doesn't arrive.
            </>
          ) : (
            <>
              Send a verification link to <strong>{user.email}</strong>.
            </>
          )}
        </>
      }
      footer={footer}
    >
      <div className="pa-auth__form" data-testid="email-verification-gate">
        {status === "sent" ? (
          <Callout tone="success" title="Verification sent">
            Check your inbox, then{" "}
            <button type="button" className="alk-link" onClick={recheck}>
              reload
            </button>{" "}
            once you've confirmed.
          </Callout>
        ) : (
          <Button fullWidth onClick={() => resend.mutate()} loading={resend.isPending}>
            {user.verification_resend_available_at != null
              ? "Resend verification email"
              : "Send verification email"}
          </Button>
        )}
        {status === "error" ? (
          <Callout tone="danger">
            {refusalSentence(resend.error, RESEND_REFUSAL)}
          </Callout>
        ) : null}
        <Button
          fullWidth
          variant="secondary" fill="ghost"
          onClick={async () => {
            await logout.mutateAsync();
            navigate("/login", { replace: true });
          }}
        >
          Log out
        </Button>
      </div>
    </AuthLayout>
  );
}

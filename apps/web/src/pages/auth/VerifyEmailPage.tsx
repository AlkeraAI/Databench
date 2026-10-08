import { useEffect } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Callout, Button, Skeleton } from "@alkera/ui";

import { AuthLayout } from "./ui/AuthLayout";
import { ApiError, refusalSentence } from "../../api/errors";
import { useVerifyEmail } from "../../api/auth";

/**
 * Confirm an account's email from the token-link in the verification email
 * (`/verify-email/:token`). The token is verified once on mount. A 409 means the token's
 * email is already verified — from the user's side that IS success (they're verified), and
 * treating it so also absorbs the duplicate request React StrictMode fires in dev. A genuine
 * bad/expired token (400) surfaces as an error.
 */
export function VerifyEmailPage() {
  const { token } = useParams<{ token: string }>();
  const navigate = useNavigate();
  const verify = useVerifyEmail();

  useEffect(() => {
    if (token && verify.status === "idle") {
      verify.mutate(token);
    }
  }, [token, verify]);

  const alreadyVerified =
    verify.isError && verify.error instanceof ApiError && verify.error.status === 409;
  const succeeded = verify.isSuccess || alreadyVerified;
  const failed = verify.isError && !alreadyVerified;

  if (succeeded) {
    return (
      <AuthLayout title="Email verified" lede="Your address is confirmed.">
        <div className="pa-auth__form">
          <Button fullWidth onClick={() => navigate("/", { replace: true })}>
            Go to dashboard
          </Button>
        </div>
      </AuthLayout>
    );
  }

  if (failed) {
    const footer = (
      <Link className="alk-link" to="/login">
        Back to sign in
      </Link>
    );
    return (
      <AuthLayout title="Couldn't verify your email" lede="This link may have expired." footer={footer}>
        <div className="pa-auth__form">
          <Callout tone="danger">
            {refusalSentence(verify.error, { fallback: "Could not verify your email." })}
          </Callout>
          <p className="pa-auth__success-body">
            Open the most recent verification email, or sign in and request a new link.
          </p>
          <Button fullWidth variant="secondary" onClick={() => navigate("/login")}>
            Go to sign in
          </Button>
        </div>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout title="Verifying your email" lede="One moment while we confirm your address.">
      <div className="pa-auth__success" role="status" aria-label="Confirming your email">
        <Skeleton width={200} height={14} />
      </div>
    </AuthLayout>
  );
}

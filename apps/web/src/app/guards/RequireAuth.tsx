import { Navigate, Outlet, useLocation } from "react-router-dom";

import { Button, PageError, Skeleton } from "@alkera/ui";

import { isProfileIncomplete, isVerificationBlocked, useCurrentUser } from "../../api/auth";
import { failureSentence } from "../../api/errors";
import { retryAfterSeconds } from "../../api/retry";

/** The gate's redirect target, carrying the attempted location as a `?return_to=`
 *  deep link. A query param (not router state) so it survives a reload and flows
 *  through the same `safeReturnTo` sanitizer as the OAuth callback's return_to.
 *  The bare home path is the post-auth default anyway, so it adds no param. */
function gateRedirect(to: string, attempted: string): string {
  return attempted === "/" ? to : `${to}?return_to=${encodeURIComponent(attempted)}`;
}

/**
 * The authed/not gate in front of the product shell. While the session resolves
 * it shows a centered placeholder (never the empty/error surface — that would
 * flash before we know who the user is); a signed-out result redirects to
 * /login, carrying the attempted location as `?return_to=` so the post-login
 * navigation can return there.
 *
 * Two further gates run in order, mirroring the signup flow:
 *   1. Profile (hard, immediate): a name-less account — the state right after a
 *      minimal signup — goes to /complete-profile before any product surface,
 *      carrying the attempted location the same way (so a funnel signup still
 *      lands on Billing after the profile step).
 *   2. Email verification (soft, grace-windowed): once the grace lapses, an
 *      unverified account goes to the full-screen gate until it confirms.
 *
 * A read that FAILED is not a sign-out. `null` means the server answered 401 and
 * the session is over; `undefined` after an error means the question never got
 * an answer, and putting a sign-in card in front of somebody whose cookie is
 * still good only sends them to retype a password into a rate limiter. So a
 * spent retry ladder gets the failed-load plate instead, and a session that
 * already answered once stays in force under a notice while the refresh is down.
 */
export function RequireAuth() {
  const { data: user, isPending, isError, error, isFetching, refetch } = useCurrentUser();
  const location = useLocation();
  const attempted = location.pathname + location.search;

  if (isPending) {
    return (
      <div className="alk-authgate" role="status" aria-label="Loading your workspace">
        <Skeleton width={220} height={14} />
      </div>
    );
  }
  if (isError && user === undefined) {
    return (
      <div className="alk-authgate">
        <PageError
          of="your session"
          message={failureSentence(error)}
          retryAfterSeconds={retryAfterSeconds(error)}
          onRetry={() => void refetch()}
        />
      </div>
    );
  }
  if (!user) {
    return <Navigate to={gateRedirect("/login", attempted)} replace />;
  }
  if (isProfileIncomplete(user) && location.pathname !== "/complete-profile") {
    return <Navigate to={gateRedirect("/complete-profile", attempted)} replace />;
  }
  if (isVerificationBlocked(user) && location.pathname !== "/verify-email-required") {
    return <Navigate to="/verify-email-required" replace />;
  }
  return (
    <>
      {isError ? (
        <div className="alk-sessionnotice" role="status">
          <span>Could not check your session.</span>
          <Button
            variant="secondary"
            fill="ghost"
            size="sm"
            disabled={isFetching}
            onClick={() => void refetch()}
          >
            Retry
          </Button>
        </div>
      ) : null}
      <Outlet />
    </>
  );
}

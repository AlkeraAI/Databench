import { Link } from "react-router-dom";

import { formatDate } from "@/lib/format/date";

import { useCurrentUser } from "../api/auth";

/** A small warning triangle in the flask's single-weight rounded line. */
function WarnTriangle() {
  return (
    <svg
      width={16}
      height={16}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 4 20.5 19.5H3.5z" />
      <path d="M12 10v4" />
      <path d="M12 16.9h.01" />
    </svg>
  );
}

/**
 * Full-width notice pinned above the product shell while the signed-in account still
 * owes a verified email. It shows during the grace window (the gate takes over once the
 * window lapses), and disappears for verified accounts and platform staff, for whom
 * `email_verification_required` is false. Returns null when not applicable.
 *
 * The action routes to the verification page (`/verify-email`) instead of resending
 * inline — that page owns the resend flow, including the server-driven cooldown
 * countdown the banner has no room to render.
 */
export function EmailVerificationBanner() {
  const { data: user } = useCurrentUser();

  if (!user || !user.email_verification_required) return null;

  const deadline = formatDate(user.email_verification_deadline) || null;
  // A resend window exists only while a link that actually went out is pending.
  const linkSent = user.verification_resend_available_at != null;

  return (
    <div className="alk-verify-banner" role="alert" data-testid="email-verification-banner">
      <span className="alk-verify-banner__icon">
        <WarnTriangle />
      </span>
      <p className="alk-verify-banner__msg">
        {linkSent ? (
          <>
            We sent a verification link to <strong>{user.email}</strong>. Open it
          </>
        ) : (
          <>
            Verify <strong>{user.email}</strong>
          </>
        )}{" "}
        to use the agent
        {deadline ? (
          <>
            {" "}
            and to keep account access past <strong>{deadline}</strong>
          </>
        ) : null}
        .{linkSent ? " Check your spam folder if it hasn't arrived." : null}
      </p>
      <Link className="alk-link alk-verify-banner__action" to="/verify-email">
        {linkSent ? "Resend verification email" : "Send verification email"}
      </Link>
    </div>
  );
}

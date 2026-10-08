import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { Button, Callout, Inline, Skeleton, Stack, ToastViewport, useToasts } from "@alkera/ui";

import { formatDate } from "@/lib/format/date";
import { useSecondsUntil } from "@/lib/useSecondsUntil";
import { meKey, useCurrentUser, useResendVerification } from "../../api/auth";
import { useRealtimeDown } from "../../api/events/status";
import { TopbarSubtitle } from "../../app/Topbar";
import styles from "./EmailVerificationPage.module.css";
import { refusalSentence } from "../../api/errors";
import { RESEND_REFUSAL } from "../auth/resendRefusal";

const RECHECK_MS = 60_000;

function cooldownLabel(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`;
}

/**
 * The email-verification page — the one place that explains the verification
 * gate and owns the resend flow. Linked from the in-grace banner, and the
 * target of the CLI / editor "open verification page" actions, so it must
 * stand alone: status, resend (with the server-driven cooldown countdown from
 * `verification_resend_available_at`), a manual re-check, and a once-a-minute
 * auto re-check for the "verified in another tab" moment.
 */
export function EmailVerificationPage() {
  const { data: user, isPending, refetch } = useCurrentUser();
  const resend = useResendVerification();
  const qc = useQueryClient();
  const { toasts, push, dismiss } = useToasts();
  const [rechecking, setRechecking] = useState(false);
  const unverified = Boolean(user?.email_verification_required);
  const cooldown = useSecondsUntil(user?.verification_resend_available_at ?? null);
  const down = useRealtimeDown();

  // The link is usually opened in ANOTHER tab (or the email client's browser). The
  // user.email_verified event flips this page over the event stream; while the stream is
  // not delivering, re-read the identity once a minute so it still flips on its own.
  useEffect(() => {
    if (!unverified || !down) return;
    const timer = setInterval(() => void qc.invalidateQueries({ queryKey: meKey }), RECHECK_MS);
    return () => clearInterval(timer);
  }, [unverified, down, qc]);

  // The manual Recheck awaits the fresh identity so it can SAY what it found —
  // a silent click that changes nothing reads as a broken button.
  const recheck = async () => {
    setRechecking(true);
    try {
      const fresh = await refetch();
      if (fresh.data && !fresh.data.email_verification_required) {
        push({ message: "Email verified.", tone: "success" });
      } else {
        push({ message: "Still unverified. Open the link in your inbox.", tone: "warning" });
      }
    } finally {
      setRechecking(false);
    }
  };
  const resendNow = () =>
    resend.mutate(undefined, {
      // The countdown is served by /me (`verification_resend_available_at`) —
      // re-read it on ANY outcome, so a success starts the fresh window and a
      // 429 (another surface just resent) resyncs the drifted countdown.
      onSettled: () => void qc.invalidateQueries({ queryKey: meKey }),
    });

  if (isPending || !user) {
    return (
      <div className={styles.doc} data-measure-surface="product">
        <TopbarSubtitle>Prove your email address to unlock model requests.</TopbarSubtitle>
        <Skeleton width={280} height={16} />
      </div>
    );
  }

  if (!unverified) {
    return (
      <div className={styles.doc} data-measure-surface="product">
        <Stack gap={4} align="start">
          <Callout tone="success" title="Your email is verified">
            <p>
              <strong>{user.email}</strong> is confirmed.
            </p>
          </Callout>
          <Link className="alk-link" to="/">
            Back to the dashboard
          </Link>
        </Stack>
        {/* A Recheck that JUST flipped the page lands its success toast here. */}
        <ToastViewport toasts={toasts} onDismiss={dismiss} position="bottom-center" />
      </div>
    );
  }

  const deadline = formatDate(user.email_verification_deadline) || null;
  // A resend window exists only while a link that actually went out is pending.
  const linkSent = user.verification_resend_available_at != null;

  return (
    <div className={styles.doc} data-measure-surface="product">
      <TopbarSubtitle>Prove your email address to unlock model requests.</TopbarSubtitle>
      <Stack gap={4} align="start">
        <Callout tone="warning" title="Verify your email">
          <p>
            {linkSent ? (
              <>
                We sent a verification link to <strong>{user.email}</strong>. Open it
              </>
            ) : (
              <>
                Send a verification link to <strong>{user.email}</strong> and open it
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
        </Callout>
        {resend.isSuccess ? (
          <Callout tone="success">Verification email sent. Check your inbox.</Callout>
        ) : null}
        {resend.isError && cooldown <= 0 ? (
          <Callout tone="danger">
            {refusalSentence(resend.error, RESEND_REFUSAL)}
          </Callout>
        ) : null}
        <Inline gap={2}>
          <Button onClick={resendNow} loading={resend.isPending} disabled={cooldown > 0}>
            {cooldown > 0
              ? `Resend available in ${cooldownLabel(cooldown)}`
              : linkSent
                ? "Resend verification email"
                : "Send verification email"}
          </Button>
          {/* Disabled (not `loading`) while in flight: the check settles in a
              blink, and the spinner swap reads as a flicker. The outcome toast
              is the feedback. */}
          <Button fill="outline" disabled={rechecking} onClick={() => void recheck()}>
            Recheck
          </Button>
        </Inline>
      </Stack>
      <ToastViewport toasts={toasts} onDismiss={dismiss} position="bottom-center" />
    </div>
  );
}

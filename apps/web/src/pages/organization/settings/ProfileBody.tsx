import { useRef, useState, type ReactNode } from "react";

import { Callout, Button, ConfirmDialog, GitHubMark, GoogleMark, Inline, Modal, Pill, Skeleton, Stack, TextInput } from "@alkera/ui";

import { Icon } from "../../../app/icons";
import type { IconName } from "../../../app/icons";
import { ApiError } from "../../../api/errors";
import { useCurrentUser, useRequestPasswordReset, type CurrentUser } from "../../../api/auth";
import {
  useIdentities,
  useLogoutAll,
  useRevokeSession,
  useSessions,
  useUpdateProfile,
  type Session,
} from "../../../api/account";
import { ActionRow, LoadError, PersonCell, SaveBar, SettingsSection, SettingsSkeleton, errText } from "./fields";
import { AccountLifecycleSections } from "./AccountLifecycleSections";
import { MfaSection } from "./MfaSection";
import {
  PROVIDERS,
  identityFor,
  sessionIcon,
  sessionMeta,
  sessionRevokeLabel,
  sessionTimes,
  sessionTitle,
} from "./data";
import shell from "./shell.module.css";
import { formatDate } from "@/lib/format/date";
import type { Notify } from "../../../app/notify";

/**
 * Profile — the person's own account, wired to the real backend:
 *  - identity: first/last name + email (PATCH /users/{id}); the email's verification mark rides the
 *    field label, and a dirty edit raises the sticky save bar;
 *  - sign-in: a password-reset link (POST /auth/password-reset/request) + the linked OAuth providers
 *    (GET /auth/identities, view-only — there is no signed-in link flow, so an unlinked provider is
 *    not offered as a button that could only pretend to connect it);
 *  - sessions: the active session / CLI-token registry (GET /auth/sessions) — revoke one
 *    (DELETE /auth/sessions/{jti}) or sign out everywhere (POST /auth/logout-all), each confirmed.
 */
type ModalState =
  | { kind: "revoke"; session: Session }
  | { kind: "signout-all" }
  // Changing the email is credential-grade: PATCH /users/{id} demands proof of a
  // CURRENT factor (a stolen session must not be able to repoint the self-service
  // reset channel). The backend owns the policy and signals what it needs through
  // the envelope `code`, so the SPA never duplicates the rule about which fields
  // are credential-grade — it submits, and prompts for whatever it is told is missing.
  | { kind: "step-up"; needMfa: boolean; error: string | null }
  | null;

const providerMark = (key: string): ReactNode => (key === "google" ? <GoogleMark size={18} /> : <GitHubMark size={18} />);

/** An icon + word status Pill. `plain` is a chip-less inline mark (Verified, CLI token); `lg` is the
 *  button-height chip for a status that replaces a row's action button (Connected, This device). */
function MarkPill({
  tone,
  icon,
  variant = "soft",
  size = "sm",
  children,
}: {
  tone: "success" | "neutral" | "warning";
  icon: IconName;
  variant?: "soft" | "plain";
  size?: "sm" | "lg";
  children: ReactNode;
}) {
  return (
    <Pill
      tone={tone}
      variant={variant}
      size={size}
      shape={size === "lg" ? "rect" : "pill"}
      icon={<Icon name={icon} size={size === "lg" ? 13 : 12} />}
    >
      {children}
    </Pill>
  );
}

export function ProfileBody({ notify }: { notify: Notify }) {
  const me = useCurrentUser();
  if (me.isLoading) return <SettingsSkeleton sections={3} />;
  if (me.isError || !me.data) return <LoadError message="We couldn't load your account details." onRetry={() => void me.refetch()} />;
  return <ProfileForm key={me.data.id} user={me.data} notify={notify} />;
}

function ProfileForm({ user, notify }: { user: CurrentUser; notify: Notify }) {
  const [firstName, setFirstName] = useState(user.first_name);
  const [lastName, setLastName] = useState(user.last_name);
  const [email, setEmail] = useState(user.email);
  const [modal, setModal] = useState<ModalState>(null);
  const [stepUpPassword, setStepUpPassword] = useState("");
  const [stepUpCode, setStepUpCode] = useState("");
  const stepUpPasswordRef = useRef<HTMLInputElement>(null);

  const update = useUpdateProfile();
  const resetPw = useRequestPasswordReset();
  const revoke = useRevokeSession();
  const logoutAll = useLogoutAll();
  const identities = useIdentities();
  const sessions = useSessions();

  const dirty = firstName !== user.first_name || lastName !== user.last_name || email !== user.email;
  const emailValid = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim());
  // The server refuses a blank name; saying so on the field beats a refusal after the round trip.
  const firstNameError = firstName.trim() ? undefined : "Enter your first name.";
  const lastNameError = lastName.trim() ? undefined : "Enter your last name.";
  const verified = Boolean(user.email_verified_at);

  const submitProfile = (proof: { current_password?: string; mfa_code?: string }) => {
    update.mutate(
      { userId: user.id, patch: { first_name: firstName, last_name: lastName, email, ...proof } },
      {
        onSuccess: (saved) => {
          setModal(null);
          setStepUpPassword("");
          setStepUpCode("");
          if (saved.email === user.email) {
            notify.success("Profile saved.");
          } else if (saved.verification_resend_available_at != null) {
            // The server reports a resend window only after the link actually went out.
            notify.success(`Profile saved. We sent a verification link to ${saved.email}.`);
          } else {
            notify.error(`Email changed, but we couldn't send a verification link to ${saved.email}. Send it again from the verification page.`);
          }
        },
        onError: (e) => {
          const code = e instanceof ApiError ? e.code : null;
          switch (code) {
            // Not a guess, just an absent factor — open the prompt and collect it.
            case "current_password_required":
              setModal({ kind: "step-up", needMfa: false, error: null });
              return;
            case "mfa_required":
              setModal({ kind: "step-up", needMfa: true, error: null });
              return;
            // A wrong factor IS counted against the lockout, so keep the prompt
            // open and say which one was wrong rather than silently retrying.
            case "current_password_invalid":
              setStepUpPassword("");
              setModal({ kind: "step-up", needMfa: false, error: errText(e, "That password was not correct.") });
              return;
            case "mfa_invalid":
              setStepUpCode("");
              setModal({ kind: "step-up", needMfa: true, error: errText(e, "That code was not correct.") });
              return;
            // A federated account has no password to prove — the emailed link is
            // the proof of possession, so point at it instead of prompting.
            case "password_not_set":
            case "account_locked":
              setModal(null);
              notify.error(errText(e, "Could not save your profile."));
              return;
            default:
              setModal(null);
              notify.error(errText(e, "Could not save your profile."));
          }
        },
      },
    );
  };

  const save = () => {
    if (!emailValid || !dirty || firstNameError || lastNameError) return;
    submitProfile({});
  };
  const discard = () => {
    setFirstName(user.first_name);
    setLastName(user.last_name);
    setEmail(user.email);
  };
  const sendPasswordLink = () =>
    resetPw.mutate(
      { email: user.email },
      {
        onSuccess: () => notify.success(`Password link sent to ${user.email}.`),
        onError: (e) => notify.error(errText(e, "Could not send the password link.")),
      },
    );

  return (
    <>
      <SettingsSection id="profile-identity" icon="user" title="Profile">
        <Inline gap={7} align="stretch" block>
          <TextInput label="First name" value={firstName} autoComplete="off" className={shell.capped} rootStyle={{ flex: "1 1 200px", minWidth: 0 }} error={firstNameError} onChange={(e) => setFirstName(e.target.value)} />
          <TextInput label="Last name" value={lastName} autoComplete="off" className={shell.capped} rootStyle={{ flex: "1 1 200px", minWidth: 0 }} error={lastNameError} onChange={(e) => setLastName(e.target.value)} />
        </Inline>
        <TextInput
          label="Email"
          type="email"
          value={email}
          autoComplete="off"
          className={shell.capped}
          labelAccessory={
            verified ? (
              <MarkPill tone="success" icon="shieldCheck" variant="plain">
                Verified
              </MarkPill>
            ) : (
              <MarkPill tone="warning" icon="alert" variant="plain">
                Unverified
              </MarkPill>
            )
          }
          error={email.trim() && !emailValid ? "Enter a valid email address." : undefined}
          onChange={(e) => setEmail(e.target.value)}
        />
      </SettingsSection>

      <SettingsSection id="profile-signin" icon="key" title="Sign-in">
        <ActionRow
          label="Password"
          help={user.has_password ? "Changed through a secure link sent to your email." : "Set one to also sign in with email."}
        >
          <Button variant="secondary" loading={resetPw.isPending} onClick={sendPasswordLink}>
            {user.has_password ? "Change password" : "Set password"}
          </Button>
        </ActionRow>

        {identities.isLoading ? (
          <RowsSkeleton rows={2} />
        ) : identities.isError ? (
          <Callout tone="danger">We couldn't load your sign-in providers.</Callout>
        ) : (
          PROVIDERS.flatMap((p) => {
            const linked = identityFor(identities.data ?? [], p.key);
            if (!linked) return [];
            return [
              <Inline gap={7} justify="space-between" wrap={false} block key={p.key}>
                <PersonCell
                  logo={providerMark(p.key)}
                  name={p.label}
                  meta={`${linked.email_at_link ?? "Linked"}${linked.last_login_at ? ` · last used ${formatDate(linked.last_login_at)}` : ""}`}
                />
                <Inline gap={3} wrap={false} style={{ flexShrink: 0 }}>
                  <MarkPill tone="success" icon="check" size="lg">
                    Connected
                  </MarkPill>
                </Inline>
              </Inline>,
            ];
          })
        )}
      </SettingsSection>

      <MfaSection notify={notify} />

      <SettingsSection
        id="profile-sessions"
        icon="device"
        title="Sessions"
        actions={
          <Button
            variant="destructive" fill="outline"
            leftSection={<Icon name="logout" size={15} />}
            disabled={!sessions.data?.some((s) => !s.current)}
            onClick={() => setModal({ kind: "signout-all" })}
          >
            Sign out everywhere
          </Button>
        }
      >
        {sessions.isLoading ? (
          <RowsSkeleton rows={3} />
        ) : sessions.isError ? (
          <Callout tone="danger">We couldn't load your sessions.</Callout>
        ) : (
          (sessions.data ?? []).map((s) => (
            <Inline gap={7} justify="space-between" wrap={false} block key={s.jti}>
              <PersonCell
                icon={sessionIcon(s)}
                name={sessionTitle(s)}
                meta={sessionMeta(s)}
                detail={sessionTimes(s)}
                badge={
                  s.token_type === "cli" ? (
                    <MarkPill tone="warning" icon="key" variant="plain">
                      CLI token
                    </MarkPill>
                  ) : undefined
                }
              />
              <Inline gap={3} wrap={false} style={{ flexShrink: 0 }}>
                {s.current ? (
                  <MarkPill tone="success" icon="check" size="lg">
                    This device
                  </MarkPill>
                ) : (
                  <Button
                    variant="destructive"
                    fill="outline"
                    aria-label={sessionRevokeLabel(s)}
                    onClick={() => setModal({ kind: "revoke", session: s })}
                  >
                    Revoke
                  </Button>
                )}
              </Inline>
            </Inline>
          ))
        )}
      </SettingsSection>

      <AccountLifecycleSections user={user} notify={notify} />

      <ConfirmDialog
        open={modal?.kind === "revoke"}
        onClose={() => setModal(null)}
        title="Revoke this session?"
        confirmLabel="Revoke session"
        tone="warning"
        busy={revoke.isPending}
        onConfirm={() => {
          if (modal?.kind === "revoke") {
            const s = modal.session;
            revoke.mutate(s.jti, {
              onSuccess: () => notify.success(`Revoked ${sessionTitle(s)}.`),
              onError: (e) => notify.error(errText(e, "Could not revoke that session.")),
            });
          }
          setModal(null);
        }}
        consequence={
          modal?.kind === "revoke" ? (
            <>
              <b>{sessionTitle(modal.session)}</b> is signed out immediately. A CLI token stops working until
              you mint a new one.
            </>
          ) : undefined
        }
      />
      <ConfirmDialog
        open={modal?.kind === "signout-all"}
        onClose={() => setModal(null)}
        title="Sign out everywhere?"
        confirmLabel="Sign out everywhere"
        tone="warning"
        busy={logoutAll.isPending}
        onConfirm={() => {
          logoutAll.mutate(undefined, {
            onSuccess: () => notify.success("Signed out of all other sessions."),
            onError: (e) => notify.error(errText(e, "Could not sign out your other sessions.")),
          });
          setModal(null);
        }}
        consequence="Every other browser session and CLI token is revoked. You stay signed in on this device."
      />
      <Modal
        open={modal?.kind === "step-up"}
        onClose={() => {
          setModal(null);
          setStepUpPassword("");
          setStepUpCode("");
        }}
        size="sm"
        title="Confirm it's you"
        confirmLabel="Confirm and save"
        // Land the cursor in the password field rather than on the dialog's first
        // focusable chrome — this prompt exists to be typed into.
        initialFocusRef={stepUpPasswordRef}
        onConfirm={() => {
          if (modal?.kind !== "step-up") return;
          submitProfile({
            current_password: stepUpPassword,
            ...(modal.needMfa ? { mfa_code: stepUpCode } : {}),
          });
        }}
      >
        <Stack gap="md">
          <p>Changing your email address needs your current password.</p>
          {modal?.kind === "step-up" && modal.error ? (
            <Callout tone="danger">{modal.error}</Callout>
          ) : null}
          <TextInput
            ref={stepUpPasswordRef}
            label="Current password"
            type="password"
            autoComplete="current-password"
            value={stepUpPassword}
            onChange={(e) => setStepUpPassword(e.target.value)}
          />
          {modal?.kind === "step-up" && modal.needMfa ? (
            <TextInput
              label="Authenticator code"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={stepUpCode}
              onChange={(e) => setStepUpCode(e.target.value)}
            />
          ) : null}
        </Stack>
      </Modal>

      <SaveBar dirty={dirty} saving={update.isPending} onSave={save} onDiscard={discard} />
    </>
  );
}

/** Skeleton rows inside an already-rendered section while its list query loads. */
function RowsSkeleton({ rows }: { rows: number }) {
  return (
    <>
      {Array.from({ length: rows }, (_, i) => (
        <Inline gap={7} justify="space-between" wrap={false} block key={i}>
          <Inline gap={5} grow wrap={false} style={{ minWidth: 0 }}>
            <Skeleton width={34} height={34} />
            <Stack>
              <Skeleton width={120} height={14} />
              <Skeleton width={180} height={12} style={{ marginTop: 4 }} />
            </Stack>
          </Inline>
          <Skeleton width={92} height={32} />
        </Inline>
      ))}
    </>
  );
}

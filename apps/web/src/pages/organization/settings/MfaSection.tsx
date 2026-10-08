import { useEffect, useRef, useState } from "react";

import { QRCodeSVG } from "qrcode.react";

import { Callout, Anchor, Button, Inline, Pill, Skeleton, Stack, TextInput, cx } from "@alkera/ui";

import { ApiError } from "../../../api/errors";
import { Icon } from "../../../app/icons";
import { type MfaEnroll, useMfaConfirm, useMfaDisable, useMfaEnroll, useMfaStatus } from "../../../api/mfa";
import { ActionRow, ReadingField, SettingsSection, errText } from "./fields";
import styles from "./MfaSection.module.css";
import type { Notify } from "../../../app/notify";

/**
 * Two-factor (TOTP) enrollment on the portal's Profile page.
 * A small state machine, each state a body inside the one MFA section:
 *   off       → "Set up" button (enroll → pending, or → step-up when the server wants the password)
 *   step-up   → a current-password field; submitting it retries enroll with the password
 *   pending   → QR + secret reading + otpauth link + a confirm-code field (confirm → backup codes)
 *   backup    → the single-use backup codes, shown exactly once (Done → off/on)
 *   on        → "two-factor is on", a code field to turn it off (disable → off)
 *
 * Mounts INSIDE the Profile body and reports outcomes through the shared `notify`: a refusal is an
 * error, never a success.
 */
export function MfaSection({ notify }: { notify: Notify }) {
  const status = useMfaStatus();
  const enroll = useMfaEnroll();
  const confirm = useMfaConfirm();
  const disable = useMfaDisable();

  const [pending, setPending] = useState<MfaEnroll | null>(null);
  const [backupCodes, setBackupCodes] = useState<string[] | null>(null);
  const [code, setCode] = useState("");
  // Set while the server wants the account password before it will mint a secret; `error` names a
  // wrong one. A missing password is a prompt, not a failure, so it is never toasted.
  const [stepUp, setStepUp] = useState<{ error: string | null } | null>(null);
  const [password, setPassword] = useState("");

  const enabled = status.data?.enabled ?? false;
  const remaining = status.data?.backup_codes_remaining ?? 0;

  const clearCode = () => setCode("");

  const startEnroll = (currentPassword?: string) =>
    enroll.mutate(currentPassword, {
      onSuccess: (d) => {
        setStepUp(null);
        setPassword("");
        setPending(d);
        clearCode();
      },
      onError: (e) => {
        const reason = e instanceof ApiError ? e.code : null;
        if (reason === "current_password_required") {
          setStepUp({ error: null });
          return;
        }
        // The server's sentence counts the tries left before the lockout; the reader needs it.
        if (reason === "current_password_invalid") {
          setPassword("");
          setStepUp({ error: errText(e, "That password isn't correct.") });
          return;
        }
        setStepUp(null);
        setPassword("");
        notify.error(errText(e, "Could not start two-factor setup."));
      },
    });

  const cancelStepUp = () => {
    setStepUp(null);
    setPassword("");
  };

  const confirmCode = () =>
    confirm.mutate(code.trim(), {
      onSuccess: (d) => {
        setBackupCodes(d.backup_codes);
        setPending(null);
        clearCode();
      },
    });

  const disableMfa = () =>
    disable.mutate(code.trim(), {
      onSuccess: () => {
        clearCode();
        notify.success("Two-factor authentication turned off.");
      },
    });

  return (
    <SettingsSection
      id="profile-mfa"
      icon="shield"
      title="Two-factor authentication"
      actions={
        enabled && !backupCodes ? (
          <Pill tone="success" variant="soft" size="sm" icon={<Icon name="shieldCheck" size={12} />}>
            On
          </Pill>
        ) : undefined
      }
    >
      {status.isPending ? (
        <Inline gap={7} justify="space-between" wrap={false} block>
          <Skeleton width={240} height={14} />
          <Skeleton width={150} height={32} />
        </Inline>
      ) : status.isError ? (
        <Callout tone="danger">We couldn't load your two-factor status.</Callout>
      ) : backupCodes ? (
        <BackupCodes
          codes={backupCodes}
          onDone={() => {
            setBackupCodes(null);
            void status.refetch();
            notify.success("Two-factor authentication is on.");
          }}
        />
      ) : enabled ? (
        <EnabledBody
          remaining={remaining}
          code={code}
          onCode={setCode}
          error={disable.error}
          busy={disable.isPending}
          onDisable={disableMfa}
        />
      ) : stepUp ? (
        <PasswordStepUp
          password={password}
          onPassword={setPassword}
          error={stepUp.error}
          busy={enroll.isPending}
          onSubmit={() => startEnroll(password)}
          onCancel={cancelStepUp}
        />
      ) : pending ? (
        <PendingBody
          enroll={pending}
          code={code}
          onCode={setCode}
          error={confirm.error}
          busy={confirm.isPending}
          onConfirm={confirmCode}
        />
      ) : (
        <ActionRow
          label="Two-factor is off"
          help="Add a one-time code from an authenticator app to your sign-in."
        >
          <Button variant="secondary" loading={enroll.isPending} onClick={() => startEnroll()}>
            Set up
          </Button>
        </ActionRow>
      )}
    </SettingsSection>
  );
}

/** The step-up: the account password the server asked for before it mints a secret. */
function PasswordStepUp({
  password,
  onPassword,
  error,
  busy,
  onSubmit,
  onCancel,
}: {
  password: string;
  onPassword: (v: string) => void;
  error: string | null;
  busy: boolean;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  const field = useRef<HTMLInputElement>(null);
  // The prompt exists to be typed into: the caret lands in it when it appears, and again after a
  // wrong password clears it.
  useEffect(() => {
    field.current?.focus();
  }, [error]);
  return (
    <Stack gap={5} align="stretch">
      <Inline
        as="form"
        aria-label="Confirm your password"
        gap={5}
        align="flex-end"
        onSubmit={(e) => {
          e.preventDefault();
          if (password) onSubmit();
        }}
      >
        <TextInput
          ref={field}
          label="Current password"
          type="password"
          autoComplete="current-password"
          value={password}
          rootStyle={{ flex: "0 1 260px" }}
          error={error ?? undefined}
          onChange={(e) => onPassword(e.target.value)}
        />
        <Button type="submit" variant="primary" loading={busy} disabled={!password}>
          Continue
        </Button>
        <Button variant="secondary" fill="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </Inline>
    </Stack>
  );
}

/** The pending state: pair the authenticator with the secret, then confirm a code. */
function PendingBody({
  enroll,
  code,
  onCode,
  error,
  busy,
  onConfirm,
}: {
  enroll: MfaEnroll;
  code: string;
  onCode: (v: string) => void;
  error: unknown;
  busy: boolean;
  onConfirm: () => void;
}) {
  return (
    <Stack gap={6} align="stretch">
      <p className={cx(styles.mfaCopy, "alk-muted")}>
        Scan the QR code or paste the key into your authenticator app, then enter the 6-digit code it shows.
      </p>
      <Inline gap={7} align="flex-start" block>
        <QrTile uri={enroll.otpauth_uri} />
        <Stack gap={5} align="stretch" style={{ flex: "1 1 260px", minWidth: 0 }}>
          <ReadingField label="Setup key" value={enroll.secret} />
          <Anchor href={enroll.otpauth_uri} className="alk-link">
            Open in your authenticator app
          </Anchor>
        </Stack>
      </Inline>
      <CodeAction
        label="6-digit code"
        code={code}
        onCode={onCode}
        actionLabel="Verify and turn on"
        busy={busy}
        onAction={onConfirm}
      />
      {error ? <Callout tone="danger">{errText(error, "That code didn't match.")}</Callout> : null}
    </Stack>
  );
}

/** The otpauth URI as a scannable QR — not in the aria-label though, since it carries the secret. */
function QrTile({ uri }: { uri: string }) {
  return (
    <div className={styles.mfaQr}>
      <QRCodeSVG
        value={uri}
        size={160}
        level="M"
        fgColor="#000000"
        bgColor="#FFFFFF"
        marginSize={0}
        role="img"
        aria-label="Two-factor setup QR code"
      />
    </div>
  );
}

/** The enabled state: show remaining backup codes and a field to turn it off. */
function EnabledBody({
  remaining,
  code,
  onCode,
  error,
  busy,
  onDisable,
}: {
  remaining: number;
  code: string;
  onCode: (v: string) => void;
  error: unknown;
  busy: boolean;
  onDisable: () => void;
}) {
  return (
    <Stack gap={6} align="stretch">
      <p className={cx(styles.mfaCopy, "alk-muted")}>
        Two-factor is protecting your sign-in. {remaining} backup {remaining === 1 ? "code" : "codes"} remaining.
        Each works once if you lose your authenticator. Enter a current code to turn it off.
      </p>
      <CodeAction
        label="Authenticator code"
        code={code}
        onCode={onCode}
        actionLabel="Turn off"
        actionVariant="destructive"
        actionFill="outline"
        busy={busy}
        onAction={onDisable}
      />
      {error ? (
        <Callout tone="danger">{errText(error, "Could not turn off two-factor.")}</Callout>
      ) : null}
    </Stack>
  );
}

/** The one-time backup codes, shown after activation and never again. */
function BackupCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  return (
    <Stack gap={6} align="stretch">
      <p className={cx(styles.mfaCopy, "alk-muted")}>
        Save these backup codes somewhere safe. Each works once if you lose your authenticator. They won't be shown
        again.
      </p>
      <div className={styles.mfaCodes} role="list" aria-label="Backup codes">
        {codes.map((c) => (
          <code className="alk-num" role="listitem" key={c}>
            {c}
          </code>
        ))}
      </div>
      <div>
        <Button variant="primary" onClick={onDone}>
          I've saved them
        </Button>
      </div>
    </Stack>
  );
}

/** A code input inline with its action button — the confirm and disable shapes share it. */
function CodeAction({
  label,
  code,
  onCode,
  actionLabel,
  actionVariant = "primary",
  actionFill = "filled",
  busy,
  onAction,
}: {
  label: string;
  code: string;
  onCode: (v: string) => void;
  actionLabel: string;
  actionVariant?: "primary" | "destructive";
  actionFill?: "filled" | "outline";
  busy: boolean;
  onAction: () => void;
}) {
  return (
    <Inline
      as="form"
      gap={5}
      align="flex-end"
      onSubmit={(e) => {
        e.preventDefault();
        if (code.trim()) onAction();
      }}
    >
      <TextInput
        label={label}
        placeholder="123456"
        inputMode="numeric"
        autoComplete="one-time-code"
        value={code}
        rootStyle={{ flex: "0 1 220px" }}
        onChange={(e) => onCode(e.target.value)}
      />
      <Button type="submit" variant={actionVariant} fill={actionFill} loading={busy} disabled={!code.trim()}>
        {actionLabel}
      </Button>
    </Inline>
  );
}

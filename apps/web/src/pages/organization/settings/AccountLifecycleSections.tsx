import { useState } from "react";

import { Button, Callout, ConfirmDialog, Stack, TextInput } from "@alkera/ui";

import { ApiError } from "../../../api/errors";
import type { CurrentUser } from "../../../api/auth";
import {
  useCancelDeletion,
  useDeletionPlan,
  useDeletionState,
  useRequestDeletion,
  type DeletionPlan,
} from "../../../api/accountLifecycle";
import { ACCOUNT_SETTINGS_SECTIONS } from "../../../app/extensions/portal";
import type { Notify } from "../../../app/notify";
import { Icon } from "../../../app/icons";
import { formatDollars, nanosToUsd } from "../../../lib/usageDisplay";
import { formatDate } from "@/lib/format/date";
import { ActionRow, SettingsSection, errText } from "./fields";

/**
 * The end of the account settings: the sections an extension registers, then "Delete account".
 *
 * The plan (GET /me/account/deletion/plan) is shown before anything is confirmed: what happens to
 * each organization, what must be settled first. Then the person types their email and proves a
 * current factor. A scheduled deletion shows its date and a cancel button.
 */
export function AccountLifecycleSections({ user, notify }: { user: CurrentUser; notify: Notify }) {
  const [sections] = useState(() => ACCOUNT_SETTINGS_SECTIONS.items());
  return (
    <>
      {sections.map(({ key, Section }) => (
        <Section key={key} user={user} notify={notify} />
      ))}
      <DeletionSection user={user} notify={notify} />
    </>
  );
}

function DeletionSection({ user, notify }: { user: CurrentUser; notify: Notify }) {
  const state = useDeletionState();
  const cancel = useCancelDeletion();
  const [open, setOpen] = useState(false);
  const scheduled = state.data ?? null;

  const cancelDeletion = () =>
    cancel.mutate(undefined, {
      onSuccess: () => notify.success("Your account will not be deleted."),
      onError: (e) => notify.error(errText(e, "Could not cancel the deletion.")),
    });

  return (
    <SettingsSection id="delete-account" icon="trash" title="Delete account">
      {scheduled ? (
        <>
          <Callout tone="warning">
            Your account will be deleted on <b>{formatDate(scheduled.purge_after)}</b>.
            {scheduled.blocked_reason ? " It is on hold until what blocks it is settled." : null}
          </Callout>
          <ActionRow label="Scheduled deletion">
            <Button variant="secondary" loading={cancel.isPending} onClick={cancelDeletion}>
              Cancel deletion
            </Button>
          </ActionRow>
        </>
      ) : (
        <ActionRow label="Delete account" help="Your account is deleted after a grace period. You can cancel until then.">
          <Button variant="destructive" fill="outline" leftSection={<Icon name="trash" size={15} />} onClick={() => setOpen(true)}>
            Delete account
          </Button>
        </ActionRow>
      )}
      {open ? <DeleteAccountDialog user={user} notify={notify} onClose={() => setOpen(false)} /> : null}
    </SettingsSection>
  );
}

function orgLine(org: DeletionPlan["orgs"][number]): string {
  if (org.fate === "close") return `${org.org_name} closes with your account. Everything in it is deleted.`;
  if (org.fate === "blocked") return `${org.org_name} needs another admin first.`;
  const parts = [`You leave ${org.org_name}.`];
  if (org.shared_items > 0) {
    parts.push(`${org.shared_items} shared ${org.shared_items === 1 ? "item moves" : "items move"} to ${org.transfer_to_name ?? "an admin"}.`);
  }
  if (org.private_items > 0) {
    parts.push(`${org.private_items} private ${org.private_items === 1 ? "item is" : "items are"} deleted.`);
  }
  return parts.join(" ");
}

const REFUSALS: Record<string, string> = {
  confirm_email_mismatch: "Type your account's email exactly.",
  current_password_invalid: "That password isn't correct.",
  mfa_invalid: "That code isn't correct.",
  reauth_required: "Sign out and sign in again, then delete your account within 10 minutes.",
};

function DeleteAccountDialog({ user, notify, onClose }: { user: CurrentUser; notify: Notify; onClose: () => void }) {
  const plan = useDeletionPlan(true);
  const requestDeletion = useRequestDeletion();
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);

  const data = plan.data;
  const needsPassword = data?.reauth === "password";
  // The typed email is the dialog's own gate (`requireTyped`); this is everything else.
  const factorsReady =
    !!data && data.can_proceed && (!needsPassword || password.length > 0) && (!data.mfa_required || code.trim().length > 0);

  const submit = () => {
    if (!factorsReady) return;
    setRefusal(null);
    requestDeletion.mutate(
      {
        confirm_email: user.email,
        current_password: needsPassword ? password : null,
        mfa_code: data?.mfa_required ? code.trim() : null,
      },
      {
        onSuccess: (scheduled) => {
          notify.success(`Your account will be deleted on ${formatDate(scheduled.purge_after)}.`);
          onClose();
        },
        onError: (e) => {
          const known = e instanceof ApiError && e.code ? REFUSALS[e.code] : undefined;
          setRefusal(known ?? errText(e, "Could not schedule the deletion."));
          if (e instanceof ApiError && e.code === "current_password_invalid") setPassword("");
          if (e instanceof ApiError && e.code === "mfa_invalid") setCode("");
        },
      },
    );
  };

  return (
    <ConfirmDialog
      open
      onClose={onClose}
      onConfirm={submit}
      title="Delete your account?"
      confirmLabel="Delete account"
      tone="destructive"
      busy={requestDeletion.isPending}
      confirmDisabled={!factorsReady}
      requireTyped={data?.can_proceed ? user.email : undefined}
    >
      <Stack gap="md">
        {plan.isLoading ? <p>Checking what deleting your account does…</p> : null}
        {plan.isError ? <Callout tone="danger">{errText(plan.error, "Could not load what deleting your account does.")}</Callout> : null}
        {data ? (
          <>
            {data.blockers.length > 0 ? (
              <Callout tone="danger" title="Settle these first">
                <ul>
                  {data.blockers.map((b) => (
                    <li key={`${b.code}:${b.org_id ?? ""}`}>{b.message}</li>
                  ))}
                </ul>
              </Callout>
            ) : null}
            <ul aria-label="What happens">
              <li>{`Your account is deleted in ${data.grace_days} days. You can cancel until then.`}</li>
              <li>Every other session, CLI token and access token is signed out now.</li>
              {data.orgs.map((org) => (
                <li key={org.org_id}>{orgLine(org)}</li>
              ))}
              {data.forfeited_credit_nanos > 0 ? (
                <li>{`Unused credit of ${formatDollars(nanosToUsd(data.forfeited_credit_nanos))} is forfeited.`}</li>
              ) : null}
              <li>Billing and audit records are kept without your name or email.</li>
            </ul>
            {data.can_proceed ? (
              <>
                {needsPassword ? (
                  <TextInput
                    label="Current password"
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                ) : (
                  <p className="alk-meta">You need to have signed in within the last 10 minutes.</p>
                )}
                {data.mfa_required ? (
                  <TextInput
                    label="Authenticator code"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                  />
                ) : null}
              </>
            ) : null}
            {refusal ? <Callout tone="danger">{refusal}</Callout> : null}
          </>
        ) : null}
      </Stack>
    </ConfirmDialog>
  );
}

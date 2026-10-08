// Shared admin registry modals, built on the @alkera/ui Modal base (which owns
// the scrim, focus trap, Esc, and enter/exit motion). Each is a controlled form:
// it validates before submit, shows the server's error inline as an Callout, and
// reports its outcome through the `onDone` callback the page wires to a toast.
//
// CreateOrg mints a house + its first admin; GrantCompute gives an org machine
// time. The credit modals are billing's (pages/organization/billing/admin/grantModals).

import { useState } from "react";

import { Callout, Inline, Modal, NumberInput, Select, TextInput } from "@alkera/ui";

import shared from "../admin.module.css";

import { refusalSentence } from "../../../../api/errors";
import { useCreateOrgMutation } from "../../../../api/admin/admin";
import { useGrantOrgComputeMutation } from "../../../../api/admin/compute";

const errMessage = (e: unknown): string => refusalSentence(e);


// ---- create org -----------------------------------------------------------

export function CreateOrgModal({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone?: (text: string, ok: boolean) => void }) {
  const create = useCreateOrgMutation();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [first, setFirst] = useState("");
  const [last, setLast] = useState("");
  const [password, setPassword] = useState("");
  const [serverError, setServerError] = useState<string | null>(null);

  const reset = () => {
    setName(""); setEmail(""); setFirst(""); setLast(""); setPassword(""); setServerError(null);
  };
  const close = () => { reset(); onClose(); };

  const passwordError = password.length > 0 && password.length < 8 ? "At least 8 characters." : undefined;
  const ready = name.trim() && email.trim() && first.trim() && last.trim() && password.length >= 8;

  const submit = () => {
    if (!ready) return;
    setServerError(null);
    create.mutate(
      { name: name.trim(), admin_email: email.trim(), admin_first_name: first.trim(), admin_last_name: last.trim(), admin_password: password },
      {
        onSuccess: () => { onDone?.(`Created ${name.trim()}.`, true); close(); },
        onError: (e) => setServerError(errMessage(e)),
      },
    );
  };

  return (
    <Modal open={open} onClose={close} size="md" title="Create a new organization" sub="The house and its first admin." confirmLabel="Create" onConfirm={submit} confirmDisabled={!ready} confirmBusy={create.isPending}>
      {serverError ? <Callout tone="danger">{serverError}</Callout> : null}
      <TextInput label="Org name" value={name} onChange={(e) => setName(e.target.value)} required autoFocus />
      <TextInput label="Initial admin email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
      <Inline gap={5} align="stretch" className={shared.formRow}>
        <TextInput label="Admin first name" value={first} onChange={(e) => setFirst(e.target.value)} required />
        <TextInput label="Admin last name" value={last} onChange={(e) => setLast(e.target.value)} required />
      </Inline>
      <TextInput type="password" label="Initial admin password" value={password} onChange={(e) => setPassword(e.target.value)} error={passwordError} required />
    </Modal>
  );
}

/** How long a fresh grant runs before it lapses. Every grant expires on purpose — a comped grant with
 *  no expiry is how a trial quietly becomes permanent free infrastructure — so the operator picks a
 *  window rather than a date, and the default is the one an onboarding actually wants. */
export const COMPUTE_WINDOWS = [
  { value: "14", label: "14 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
] as const;

/** The expiry a window of `days` produces, as the ISO instant the API takes. Exported so the test
 *  asserts the sent value against the same arithmetic the form uses. */
export function computeExpiry(days: number, now: Date = new Date()): string {
  return new Date(now.getTime() + days * 24 * 60 * 60 * 1000).toISOString();
}

/** Grant an org compute: how many machines it may run at once, and until when.
 *
 *  The write is idempotent on the org — granting twice raises the ceiling instead of stacking a second
 *  row — so this doubles as the "raise their limit" form. The rate is what the CUSTOMER is billed per
 *  minute; 0 is comped, which is what an onboarding normally wants (we still meter our own cost). */
export function GrantComputeModal({
  open,
  onClose,
  orgId,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  orgId: string;
  onDone?: (text: string, ok: boolean) => void;
}) {
  const grant = useGrantOrgComputeMutation(orgId);
  const [ceiling, setCeiling] = useState("1");
  const [days, setDays] = useState<string>("30");
  const [rate, setRate] = useState("0");
  const [note, setNote] = useState("");
  const [serverError, setServerError] = useState<string | null>(null);

  const reset = () => { setCeiling("1"); setDays("30"); setRate("0"); setNote(""); setServerError(null); };
  const close = () => { reset(); onClose(); };
  const machines = Number(ceiling);
  const ready = ceiling.trim().length > 0 && Number.isInteger(machines) && machines >= 0 && Number(rate) >= 0;

  const submit = () => {
    if (!ready) return;
    setServerError(null);
    grant.mutate(
      {
        ceiling: machines,
        rate_per_minute_nanos: Math.round(Number(rate)),
        expires_at: computeExpiry(Number(days)),
        note: note.trim(),
      },
      {
        onSuccess: () => {
          onDone?.(`Granted ${machines} ${machines === 1 ? "machine" : "machines"} for ${days} days.`, true);
          close();
        },
        onError: (e) => setServerError(errMessage(e)),
      },
    );
  };

  return (
    <Modal open={open} onClose={close} size="sm" title="Grant compute" confirmLabel="Grant" onConfirm={submit} confirmDisabled={!ready} confirmBusy={grant.isPending}>
      {serverError ? <Callout tone="danger">{serverError}</Callout> : null}
      <NumberInput label="Concurrent machines" placeholder="1" value={ceiling} onValueChange={setCeiling} required autoFocus />
      <Select label="Expires in" value={days} onChange={(e) => setDays(e.target.value)}>
        {COMPUTE_WINDOWS.map((w) => (
          <option key={w.value} value={w.value}>{w.label}</option>
        ))}
      </Select>
      <NumberInput label="Billed rate per minute (nano-USD)" placeholder="0" value={rate} onValueChange={setRate} />
      <TextInput label="Note" placeholder="Design-partner onboarding" value={note} onChange={(e) => setNote(e.target.value)} />
    </Modal>
  );
}

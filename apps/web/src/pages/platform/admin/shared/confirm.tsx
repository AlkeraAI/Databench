// The register's rename modal, and the one sentence every ban confirmation says.
// Asking "are you sure" is not here: every confirmation in the product goes
// through `ConfirmDialog` from @alkera/ui, so an operator meets the same dialog,
// the same key behaviour and the same guarantees wherever the register prompts.

import { useEffect, useState } from "react";

import { Modal, TextInput } from "@alkera/ui";

/** A single-field rename. Seeds the input from `current` each time it opens and
 *  disables submit until the value is non-empty and actually changed. */
export function RenameModal({
  open,
  onClose,
  title,
  label,
  current,
  busy,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  label: string;
  current: string;
  busy?: boolean;
  onSubmit: (name: string) => void;
}) {
  const [name, setName] = useState(current);
  useEffect(() => {
    if (open) setName(current);
  }, [open, current]);
  const trimmed = name.trim();
  const ready = trimmed.length > 0 && trimmed !== current;
  return (
    <Modal open={open} onClose={onClose} size="sm" title={title} confirmLabel="Save" confirmBusy={busy} confirmDisabled={!ready} onConfirm={() => ready && onSubmit(trimmed)}>
      <TextInput label={label} value={name} onChange={(e) => setName(e.target.value)} autoFocus />
    </Modal>
  );
}

/** What banning an account does, said the same way wherever a ban is confirmed —
 *  the register's two forms and the user detail page. */
export const BAN_EFFECT =
  "They will be signed out everywhere and every sign-in will fail as if the account did not exist.";

/** The verb each ban confirmation acts under. Shared with the tests so the key an
 *  operator presses and the key they assert on cannot drift apart. */
export const BAN_KEYS = { account: "Ban account", domain: "Ban domain" } as const;

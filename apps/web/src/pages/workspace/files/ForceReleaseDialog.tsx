// Why a box's lease is being taken back.
//
// Forcing a box off a chat's or a workspace's folder cuts off a turn that may
// be in flight, so the server asks for a reason and keeps it on record. The
// dialog asks for it, and Take back stays shut until there is one.

import { useRef, useState, type ReactElement } from "react";

import { ConfirmDialog, TextInput } from "@alkera/ui";

export const FORCE_RELEASE_TITLE = "Take back";
export const FORCE_RELEASE_CONSEQUENCE = "The machine stops working in this folder.";
export const FORCE_RELEASE_REASON = "Reason";
/** The longest reason the server keeps. */
export const FORCE_RELEASE_REASON_MAX = 500;

/** The lease purposes a box holds. Forcing one of these off needs a reason. */
const BOX_PURPOSES: ReadonlySet<string> = new Set(["chat", "workspace"]);

/** Whether forcing off a lease of this purpose asks for a reason. */
export function forceReleaseNeedsReason(purpose: string | null | undefined): boolean {
  return purpose !== null && purpose !== undefined && BOX_PURPOSES.has(purpose);
}

export interface ForceReleaseDialogProps {
  open: boolean;
  subjectName: string;
  busy: boolean;
  onClose(): void;
  onConfirm(reason: string): void;
}

export function ForceReleaseDialog({
  open,
  subjectName,
  busy,
  onClose,
  onConfirm,
}: ForceReleaseDialogProps): ReactElement {
  const [reason, setReason] = useState("");
  const field = useRef<HTMLInputElement | null>(null);
  const given = reason.trim();
  const close = (): void => {
    setReason("");
    onClose();
  };
  return (
    <ConfirmDialog
      open={open}
      onClose={close}
      onConfirm={() => {
        if (given === "") return;
        onConfirm(given);
        setReason("");
      }}
      title={`${FORCE_RELEASE_TITLE} ${subjectName}?`}
      consequence={FORCE_RELEASE_CONSEQUENCE}
      confirmLabel={FORCE_RELEASE_TITLE}
      tone="destructive"
      busy={busy}
      confirmDisabled={given === ""}
      initialFocusRef={field}
    >
      <TextInput
        ref={field}
        label={FORCE_RELEASE_REASON}
        value={reason}
        maxLength={FORCE_RELEASE_REASON_MAX}
        onChange={(event) => setReason(event.target.value)}
      />
    </ConfirmDialog>
  );
}

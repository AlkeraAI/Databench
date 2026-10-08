import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode, type RefObject } from "react";

import { Button } from "../../controls/Button";
import { AlertTriangleIcon } from "../../icons";
import { TextInput } from "../../inputs/TextInput";
import { Modal } from "../Modal";

/**
 * ConfirmDialog — the one surface a setting uses to ask "are you sure", so every prompt in the
 * product reads the same and carries the same guarantees. A caller supplies the fact and the verb;
 * the dialog owns everything else.
 *
 * The contract a caller can rely on:
 *  - nothing happens until `onConfirm` fires, so a control must not apply its change on click;
 *  - the scrim, the ✕, Cancel and Escape all resolve to `onClose` and never confirm;
 *  - Enter confirms only where confirming is safe — never on a `destructive` tone, where focus
 *    starts on Cancel instead;
 *  - `requireTyped` holds the primary action disabled until that exact word is typed, and
 *    `confirmDisabled` does the same for a gate the caller owns (a reason that must be given);
 *  - focus is trapped inside the dialog and returns to the opener (inherited from Modal), and
 *    never lands on an action the gate is holding shut.
 *
 * `tone` is the whole visual vocabulary: `default` for a reversible change, `warning` for one with
 * a consequence worth reading, `destructive` for a deletion. A lockout — a change that can leave
 * nobody able to get back in — is `destructive` plus a `requireTyped` gate.
 */

export type ConfirmTone = "default" | "warning" | "destructive";

export interface ConfirmDialogProps {
  open: boolean;
  /** Cancel, the ✕, the scrim and Escape all land here. Must not apply the change. */
  onClose: () => void;
  /** The primary action. Runs only when the person presses it (or Enter on a non-destructive tone). */
  onConfirm: () => void;
  /** The fact, in one sentence — the dialog's heading and its accessible name. */
  title: string;
  /** What follows from confirming, in one sentence. Omit when the title already says it. */
  consequence?: ReactNode;
  /** The action verb on the primary button — "Disable", "Remove", "Empty trash". */
  confirmLabel: string;
  /** Default "Cancel". */
  cancelLabel?: string;
  tone?: ConfirmTone;
  /** The action is in flight: the button spins and refuses a second press. */
  busy?: boolean;
  /** Hold the primary action disabled until this exact word is typed (surrounding space ignored). */
  requireTyped?: string;
  /** The caller's own gate on the primary action — a reason still to be written, a change that
   *  can't be asked for yet. Holds the action shut exactly as `requireTyped` does. */
  confirmDisabled?: boolean;
  /** Focus this on open instead of the action or Cancel — for a dialog whose body must be filled
   *  in before it can be answered. */
  initialFocusRef?: RefObject<HTMLElement | null>;
  /** Anything the caller needs below the consequence line — a list, a second paragraph. */
  children?: ReactNode;
}

/** Each tone's primary button. A destructive action is filled so it reads as the loud one; a
 *  warning is the same colour, outlined, because it is consequential but not a deletion. */
const TONE_BUTTON: Record<ConfirmTone, { variant: "primary" | "destructive"; fill: "filled" | "outline" }> = {
  default: { variant: "primary", fill: "filled" },
  warning: { variant: "destructive", fill: "outline" },
  destructive: { variant: "destructive", fill: "filled" },
};

export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  consequence,
  confirmLabel,
  cancelLabel = "Cancel",
  tone = "default",
  busy = false,
  requireTyped,
  confirmDisabled = false,
  initialFocusRef,
  children,
}: ConfirmDialogProps) {
  const [typed, setTyped] = useState("");
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const typedRef = useRef<HTMLInputElement>(null);

  // A gate half-typed and then abandoned must not carry into the next time the dialog is asked.
  useEffect(() => {
    if (open) setTyped("");
  }, [open]);

  const gateMet = (requireTyped === undefined || typed.trim() === requireTyped) && !confirmDisabled;
  const ready = gateMet && !busy;

  // Enter from the body (the typed field, say) is a shortcut only where confirming is safe — a
  // destructive dialog is answered deliberately. A focused button or link already answers Enter
  // itself, and a textarea takes it as a newline, so those keep their own behaviour.
  const onBodyKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Enter" || e.defaultPrevented || tone === "destructive" || !ready) return;
    if ((e.target as HTMLElement | null)?.closest("button,a,textarea")) return;
    e.preventDefault();
    onConfirm();
  };

  const button = TONE_BUTTON[tone];

  // Where the dialog opens focus. A caller that says so wins; a typed gate takes the cursor into
  // the field; a destructive question opens with Cancel under the finger. Otherwise it opens on
  // its action — unless the action is shut, which cannot take focus at all, so Cancel does.
  const openFocus = requireTyped !== undefined ? typedRef : tone === "destructive" || confirmDisabled ? cancelRef : confirmRef;

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="sm"
      title={title}
      icon={tone === "default" ? undefined : <AlertTriangleIcon size={20} />}
      className="alk-confirm"
      initialFocusRef={initialFocusRef ?? openFocus}
      footer={
        <>
          <Button
            ref={cancelRef}
            variant="secondary"
            fill="ghost"
            onClick={onClose}
            // A destructive dialog opens with Cancel under the finger, so a person who
            // answers the previous prompt and keeps typing hits Enter on it and loses
            // the question instead of being asked. Enter does nothing here; the ✕, the
            // scrim, Escape and a deliberate press still cancel.
            onKeyDown={(e) => {
              if (tone === "destructive" && e.key === "Enter") e.preventDefault();
            }}
          >
            {cancelLabel}
          </Button>
          <Button
            ref={confirmRef}
            variant={button.variant}
            fill={button.fill}
            loading={busy}
            disabled={!gateMet}
            onClick={onConfirm}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="alk-confirm__body" data-tone={tone} onKeyDown={onBodyKeyDown}>
        {consequence ? <p>{consequence}</p> : null}
        {children}
        {requireTyped !== undefined ? (
          <TextInput
            ref={typedRef}
            label={`Type ${requireTyped} to confirm`}
            value={typed}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setTyped(e.target.value)}
          />
        ) : null}
      </div>
    </Modal>
  );
}

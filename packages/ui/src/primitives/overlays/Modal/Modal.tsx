import { useCallback, useId, useRef, type CSSProperties, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

import { cx } from "../../cx";
import { useFocusTrap, usePresence } from "../../../hooks";
import { Button, type ButtonFill, type ButtonVariant } from "../../controls/Button";
import { CloseIcon } from "../../icons";

/**
 * Modal — the unified centre dialog.
 *
 * The floor is built in and a caller cannot opt out of it: a dimmed backdrop ALWAYS,
 * an enter AND exit animation (via `usePresence` + `data-state`), a focus trap + return, and
 * three ways to dismiss — the scrim, the close icon, and anything the caller wires to `onClose`
 * (a Cancel button in the body). Escape is on by default and gated by `escape`.
 *
 * Rendered through a portal to `document.body` so it escapes any transformed / scroll-clipping
 * ancestor and overlays the whole viewport (it is `position: fixed`). The ui `--alk*` tokens
 * are global on `:root`, so a plain `createPortal(surface, document.body)` resolves them.
 */

export type ModalSize = "sm" | "md" | "lg" | "xl" | "full";

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  /** Dialog heading. Omit for a bare surface and point `labelledBy` at your own heading. */
  title?: string;
  /** A leading glyph beside the title, vertically centred with it. */
  icon?: ReactNode;
  /** A sub-line under the title. */
  sub?: string;
  /** sm = a confirm, md = a form, lg = a wide form, full = a surface for LOOKING at
   *  something (a file preview) rather than filling it in. */
  size?: ModalSize;
  /** Escape closes the dialog. Default true. */
  escape?: boolean;
  /** Focus this on open instead of the first focusable child. */
  initialFocusRef?: RefObject<HTMLElement | null>;
  /** Accessible name when `title` is omitted — the id of a heading the caller renders. */
  labelledBy?: string;
  /** Extra class on the dialog surface, for page-specific overrides. */
  className?: string;
  /** Inline style on the dialog surface (e.g. a one-off width). */
  style?: CSSProperties;
  /** A CUSTOM footer node — overrides the confirm/cancel convenience props below. Use this only for
   *  a non-standard action row; otherwise prefer `confirmLabel` + friends. */
  footer?: ReactNode;
  /** Label for the primary action button. Setting it renders the standard footer (a Cancel button +
   *  this action); omit it (and `footer`) for a bare dialog with no footer. */
  confirmLabel?: string;
  /** Handler for the primary action button. */
  onConfirm?: () => void;
  /** The primary action's colour — `primary` (default), `destructive` for a delete, `secondary`. */
  confirmVariant?: ButtonVariant;
  /** The primary action's fill — `filled` (default), `outline` for a quiet destructive confirm. */
  confirmFill?: ButtonFill;
  /** Disable the confirm button (e.g. an incomplete form). */
  confirmDisabled?: boolean;
  /** Show a spinner on the confirm button (an in-flight submit); also disables it. */
  confirmBusy?: boolean;
  /** Cancel button label (default "Cancel"); pass `null` to omit the cancel button entirely. */
  cancelLabel?: string | null;
  /** Draw a hairline between the body and the footer. Off by default — turn it on for a long,
   *  scrolling body where the rule anchors the pinned action row. */
  footerDivided?: boolean;
  /** Body content. */
  children: ReactNode;
}

export function Modal({
  open,
  onClose,
  title,
  icon,
  sub,
  size = "md",
  escape = true,
  initialFocusRef,
  labelledBy,
  className,
  style,
  footer,
  confirmLabel,
  onConfirm,
  confirmVariant = "primary",
  confirmFill = "filled",
  confirmDisabled,
  confirmBusy,
  cancelLabel,
  footerDivided = false,
  children,
}: ModalProps) {
  const { mounted, state } = usePresence(open, 180);
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useFocusTrap(dialogRef, { active: open && mounted, onClose, escapeClosable: escape, initialFocusRef });

  const onScrimDown = useCallback(
    (e: React.MouseEvent) => {
      // preventDefault stops the mousedown from blurring focus to <body>, so the focus trap's
      // restore lands on the opener rather than nowhere.
      if (e.target === e.currentTarget) {
        e.preventDefault();
        onClose();
      }
    },
    [onClose],
  );

  if (!mounted) return null;

  // The standard footer — a Cancel button beside the primary action — is built from the convenience
  // props; a custom `footer` overrides it, and with neither there's no footer.
  const builtinFooter =
    confirmLabel != null ? (
      <>
        {cancelLabel !== null ? (
          <Button variant="secondary" fill="ghost" onClick={onClose}>
            {cancelLabel ?? "Cancel"}
          </Button>
        ) : null}
        <Button variant={confirmVariant} fill={confirmFill} onClick={onConfirm} disabled={confirmDisabled} loading={confirmBusy}>
          {confirmLabel}
        </Button>
      </>
    ) : null;
  const footerNode = footer ?? builtinFooter;

  const surface = (
    <div className="alk-modal-scrim" data-state={state} onMouseDown={onScrimDown}>
      <div
        ref={dialogRef}
        className={cx("alk-modal", className)}
        data-size={size}
        style={style}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : labelledBy}
      >
        {title ? (
          <header className="alk-modal__head">
            <div className="alk-modal__id">
              {icon ? (
                <span className="alk-modal__icon" aria-hidden="true">
                  {icon}
                </span>
              ) : null}
              <div className="alk-modal__heading">
                <h2 className="alk-modal__title" id={titleId}>
                  {title}
                </h2>
                {sub ? <p className="alk-modal__sub">{sub}</p> : null}
              </div>
            </div>
            <Button iconOnly variant="secondary" fill="ghost" size="md" className="alk-modal__x" aria-label="Close" onClick={onClose}>
              <CloseIcon size={18} />
            </Button>
          </header>
        ) : null}
        <div className="alk-modal__body">{children}</div>
        {footerNode ? <footer className={cx("alk-modal__foot", footerDivided && "alk-modal__foot--divided")}>{footerNode}</footer> : null}
      </div>
    </div>
  );

  return createPortal(surface, document.body);
}

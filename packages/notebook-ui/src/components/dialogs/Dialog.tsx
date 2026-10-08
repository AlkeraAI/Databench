// The modal frame every notebook dialog shares: a labelled dialog that keeps
// focus inside while open, cancels on Escape, and hands focus back to where it
// was when it closes.

import { useEffect, useId, useRef } from "react";
import type { KeyboardEvent, ReactNode, RefObject } from "react";
import "./dialogs.css";

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface DialogProps {
  title: ReactNode;
  children?: ReactNode;
  /** The buttons, last in tab order. */
  actions: ReactNode;
  onCancel: () => void;
  /** What takes focus on open; the first focusable element otherwise. */
  initialFocus?: RefObject<HTMLElement | null>;
}

export function Dialog({ title, children, actions, onCancel, initialFocus }: DialogProps) {
  const titleId = useId();
  const bodyId = useId();
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const target = initialFocus?.current ?? ref.current?.querySelector<HTMLElement>(FOCUSABLE) ?? ref.current;
    target?.focus();
    return () => previous?.focus();
  }, [initialFocus]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onCancel();
      return;
    }
    if (event.key !== "Tab" || !ref.current) return;
    const items = [...ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (items.length === 0) {
      event.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !ref.current.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !ref.current.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="nb-dialog-backdrop">
      <div
        ref={ref}
        className="nb-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={children ? bodyId : undefined}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <h2 id={titleId} className="nb-dialog__title">
          {title}
        </h2>
        {children ? (
          <div id={bodyId} className="nb-dialog__body">
            {children}
          </div>
        ) : null}
        <div className="nb-dialog__actions">{actions}</div>
      </div>
    </div>
  );
}

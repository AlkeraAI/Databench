import type { CSSProperties, ReactNode } from "react";

import { cx } from "../../cx";
import { AlertTriangleIcon } from "../../icons";

export interface FieldShellProps {
  /** The wrapped control's id — the label points at it. */
  htmlFor: string;
  label?: ReactNode;
  /** The label element's own id, for a control that names itself by reference
   *  (a menu-button select composes this id with its own so it announces the
   *  field AND its current value). */
  labelId?: string;
  /** A trailing slot on the label row — a status mark or small action beside the label. Requires a
   *  `label`; with no label it is not rendered. */
  labelAccessory?: ReactNode;
  description?: ReactNode;
  /** Render the description ABOVE the control (between label and control) instead of below it —
   *  for a field whose helper is a precondition to read before the input (e.g. "set by support"). */
  descriptionAbove?: boolean;
  descId?: string;
  error?: ReactNode;
  errId?: string;
  required?: boolean;
  /** Show the required mark (the asterisk beside the label). Default `true`. Pass `false` for a
   *  form whose fields are all required and where the mark would only add noise — the control keeps
   *  its `required` semantics either way; only the visual mark is dropped. */
  requiredMark?: boolean;
  className?: string;
  style?: CSSProperties;
  children: ReactNode;
}

/** The shared label / description / error scaffold every form control reuses, so
 *  the wrapper markup and its a11y wiring live in one place. With no label,
 *  description, or error it renders the control bare — a plain input stays plain. */
export function FieldShell({
  htmlFor,
  label,
  labelId,
  labelAccessory,
  description,
  descriptionAbove,
  descId,
  error,
  errId,
  required,
  requiredMark = true,
  className,
  style,
  children,
}: FieldShellProps) {
  if (!label && !description && !error) return <>{children}</>;
  const labelEl = label ? (
    <label htmlFor={htmlFor} id={labelId} className="alk-field__label">
      {label}
      {required && requiredMark ? (
        <span className="alk-field__req" aria-hidden="true">
          *
        </span>
      ) : null}
    </label>
  ) : null;
  const descEl = description ? (
    <p id={descId} className="alk-field__desc">
      {description}
    </p>
  ) : null;
  return (
    <div className={cx("alk-field", className)} style={style}>
      {labelEl && labelAccessory ? (
        <div className="alk-field__labelrow">
          {labelEl}
          <span className="alk-field__label-accessory">{labelAccessory}</span>
        </div>
      ) : (
        labelEl
      )}
      {descriptionAbove ? descEl : null}
      {children}
      {descriptionAbove ? null : descEl}
      {error ? (
        <p id={errId} className="alk-field__error">
          <AlertTriangleIcon size={13} className="alk-field__error-icon" />
          <span>{error}</span>
        </p>
      ) : null}
    </div>
  );
}

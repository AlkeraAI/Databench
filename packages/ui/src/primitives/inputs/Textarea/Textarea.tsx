import { forwardRef, type CSSProperties, type ReactNode, type TextareaHTMLAttributes } from "react";

import { cx } from "../../cx";
import { FieldShell, useFieldIds } from "../Field";
import type { ControlSize } from "../../sizes";

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: ReactNode;
  /** A trailing slot on the label row — e.g. a reveal toggle beside the label. */
  labelAccessory?: ReactNode;
  description?: ReactNode;
  error?: ReactNode;
  /** Show the required mark beside the label. Default `true`; see `FieldShellProps.requiredMark`. */
  requiredMark?: boolean;
  /** Control size — scales the horizontal padding to match the input ladder (the height is rows-
   *  driven). `lg` is the default. */
  size?: ControlSize;
  /** Class for the field wrapper; the textarea keeps `className`. Applied to the textarea itself when
   *  the field renders bare (no label/description/error — it IS the root then), never dropped. */
  rootClassName?: string;
  /** Inline style for the same wrapper `rootClassName` targets (TextInput parity). */
  rootStyle?: CSSProperties;
}

/** A multi-line text field on the control surface. */
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  {
    className,
    rootClassName,
    rootStyle,
    rows = 3,
    size = "lg",
    label,
    labelAccessory,
    description,
    error,
    required,
    requiredMark,
    id: idProp,
    style,
    "aria-describedby": ariaDescribedBy,
    ...rest
  },
  ref,
) {
  const { id, descId, errId, describedBy } = useFieldIds(idProp, { description, error });
  // Bare FieldShell renders children unwrapped — the root props land on the textarea itself.
  const bare = !label && !description && !error;
  return (
    <FieldShell
      htmlFor={id}
      label={label}
      labelAccessory={labelAccessory}
      description={description}
      descId={descId}
      error={error}
      errId={errId}
      required={required}
      requiredMark={requiredMark}
      className={rootClassName}
      style={rootStyle}
    >
      <textarea
        ref={ref}
        id={id}
        rows={rows}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={cx(ariaDescribedBy, describedBy) || undefined}
        className={cx("alk-input", "alk-textarea", bare && rootClassName, className)}
        style={bare && rootStyle ? { ...rootStyle, ...style } : style}
        data-size={size !== "lg" ? size : undefined}
        {...rest}
      />
    </FieldShell>
  );
});

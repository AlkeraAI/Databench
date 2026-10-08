import type { InputHTMLAttributes, ReactNode, Ref } from "react";

import { cx } from "../../cx";
import type { ControlSize } from "../../sizes";

// A checkbox with the brand check mark. A styled native <input type="checkbox"> — keyboard,
// indeterminate-via-ref, and form semantics come for free; only the paint is ours. Controlled by
// the caller (checked + onChange).
export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size"> {
  /** A visible label beside the box (wraps both in a <label>, the box aligned to the first line).
   *  Omit it for a bare box and pass `aria-label` instead. Mirrors Switch's built-in label. */
  label?: ReactNode;
  /** Box size — `sm` 14 / `md` 16 (default) / `lg` 20. */
  size?: ControlSize;
  /** The underlying input, for the one thing no attribute can express: a
   *  select-all box that is neither checked nor unchecked sets `indeterminate`
   *  on the element itself. */
  ref?: Ref<HTMLInputElement>;
}

export function Checkbox({ className, label, size = "md", ...rest }: CheckboxProps) {
  const input = (
    <input
      type="checkbox"
      className={cx("alk-checkbox", className)}
      data-size={size !== "md" ? size : undefined}
      {...rest}
    />
  );
  if (label == null) return input;
  return (
    <label className="alk-checkbox-field">
      {input}
      <span className="alk-checkbox-field__label">{label}</span>
    </label>
  );
}

import { forwardRef, type InputHTMLAttributes, type ReactNode } from "react";

import { cx } from "../../cx";
import type { ControlSize } from "../../sizes";

export interface SwitchProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size"> {
  /** Optional text shown beside the track. */
  label?: ReactNode;
  /** Toggle size — `sm` / `md` (default) / `lg`. A toggle isn't a control-row member, so md is its
   *  natural resting size (not lg). */
  size?: ControlSize;
}

/** A styled checkbox toggle. The track + thumb are the visual; the input stays
 *  the real, accessible control underneath. Deliberately a standalone toggle with an
 *  inline label, not a FieldShell-wired field — it carries no description/error slot
 *  (a toggle's two states rarely need inline validation). Use a TextInput-family
 *  control where field-level description/error is required. */
export const Switch = forwardRef<HTMLInputElement, SwitchProps>(function Switch(
  { className, label, size = "md", disabled, ...rest },
  ref,
) {
  return (
    <label
      className={cx("alk-switch", className)}
      data-size={size !== "md" ? size : undefined}
      data-disabled={disabled ? "" : undefined}
    >
      <input ref={ref} type="checkbox" className="alk-switch__input" disabled={disabled} {...rest} />
      <span className="alk-switch__track">
        <span className="alk-switch__thumb" />
      </span>
      {label ? <span className="alk-switch__label">{label}</span> : null}
    </label>
  );
});

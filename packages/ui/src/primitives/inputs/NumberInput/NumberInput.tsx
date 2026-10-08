import { forwardRef, type ReactNode } from "react";

import { cx } from "../../cx";
import { TextInput, type TextInputProps } from "../TextInput";

/**
 * How the field constrains entry:
 *  - `integer` — digits only (a count, a number of days); everything else is dropped.
 *  - `number`  — the permissive native numeric input: decimals, signs, and scientific `e` all typeable.
 *  - `cash`    — a money amount: a leading `$`, digits and a single decimal point capped at 2 places.
 */
export type NumberInputMode = "integer" | "number" | "cash";

export interface NumberInputProps
  extends Omit<TextInputProps, "type" | "inputMode" | "value" | "onChange" | "leftSection"> {
  /** The entry constraint (default `number`). */
  mode?: NumberInputMode;
  /** The controlled string value. */
  value: string;
  /** Fires with the sanitized string on every edit, so the parent holds exactly what the field shows. */
  onValueChange: (value: string) => void;
  /** Override the leading affix (`cash` defaults to `$`; pass `null` to drop it). */
  leftSection?: ReactNode;
  /** Fractional digits `cash` allows (default 2 — cents). A per-1M-token price needs 4. */
  decimals?: number;
}

/** Keep digits only. */
export function sanitizeDigits(raw: string): string {
  return raw.replace(/[^0-9]/g, "");
}

/** Digits and a single decimal point, capped at `decimals` fractional places (default 2). */
export function sanitizeCash(raw: string, decimals = 2): string {
  let v = raw.replace(/[^0-9.]/g, "");
  const dot = v.indexOf(".");
  if (dot !== -1) v = v.slice(0, dot + 1) + v.slice(dot + 1).replace(/\./g, "");
  const [whole, dec] = v.split(".");
  return dec !== undefined ? `${whole}.${dec.slice(0, decimals)}` : v;
}

/**
 * NumberInput — the one numeric field, over TextInput. `mode` picks the constraint (`integer`,
 * `number`, or `cash`); the value is a controlled string and `onValueChange` fires with the
 * sanitized text. Every TextInput scaffold prop (label, description, error, size) passes through.
 */
export const NumberInput = forwardRef<HTMLInputElement, NumberInputProps>(function NumberInput(
  { mode = "number", value, onValueChange, leftSection, className, decimals = 2, ...rest },
  ref,
) {
  const cls = cx("alk-num", className);
  if (mode === "number") {
    return (
      <TextInput
        ref={ref}
        type="number"
        className={cls}
        value={value}
        onChange={(e) => onValueChange(e.currentTarget.value)}
        leftSection={leftSection}
        {...rest}
      />
    );
  }
  const sanitize = mode === "cash" ? (raw: string) => sanitizeCash(raw, decimals) : sanitizeDigits;
  const lead = leftSection !== undefined ? leftSection : mode === "cash" ? <span className="alk-num">$</span> : null;
  return (
    <TextInput
      ref={ref}
      type="text"
      inputMode={mode === "cash" ? "decimal" : "numeric"}
      className={cls}
      value={value}
      onChange={(e) => onValueChange(sanitize(e.currentTarget.value))}
      leftSection={lead}
      {...rest}
    />
  );
});

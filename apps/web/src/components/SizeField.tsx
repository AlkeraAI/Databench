// One control for writing a storage ceiling — a figure plus the unit it is in,
// with the exact byte count it commits shown underneath.
//
// The byte count is not decoration. A ceiling is enforced in bytes and the units
// here are decimal (a terabyte is 1,000,000,000,000 bytes), so an operator
// typing "2" against TB is committing 2,000,000,000,000 — the help line is the
// only place they can see that before they save, and it is what makes a
// mistyped figure obvious. The text is kept exactly as typed: a figure that is
// not a plain number is refused with a reason in the same line, never rewritten
// into a different figure.

import { SegmentedControl, Stack, TextInput } from "@alkera/ui";

import { LIMIT_UNITS, exactBytes, readSize, type SizeUnit } from "@/lib/format/bytes";

export interface SizeFieldProps {
  /** The field's visible + accessible label. Carries the subject's name so a
   *  table of these never reads as a column of identical "Storage limit"s. */
  label: string;
  amount: string;
  unit: SizeUnit;
  onAmount: (v: string) => void;
  onUnit: (u: SizeUnit) => void;
  disabled?: boolean;
  /** Width of the figure input; a table cell wants a narrower one than a card. */
  width?: number;
}

export function SizeField({ label, amount, unit, onAmount, onUnit, disabled, width = 120 }: SizeFieldProps) {
  const entry = readSize(amount, unit);
  const hintId = `size-field-hint-${label.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <Stack gap={2} align="stretch" style={{ flex: "0 0 auto" }}>
      <div style={{ display: "flex", gap: "var(--alkSpace3)", alignItems: "flex-end" }}>
        <TextInput
          label={label}
          inputMode="decimal"
          placeholder="0"
          className="alk-num"
          rootStyle={{ width }}
          disabled={disabled}
          value={amount}
          aria-invalid={entry.reason != null || undefined}
          aria-describedby={hintId}
          onChange={(e) => onAmount(e.target.value)}
        />
        <SegmentedControl
          size="md"
          label={`Unit for ${label}`}
          semantics="radio"
          value={unit}
          onChange={(k) => onUnit(k as SizeUnit)}
          options={LIMIT_UNITS.map((u) => ({ key: u, label: u }))}
        />
      </div>
      {entry.value != null ? (
        <span id={hintId} className="alk-meta alk-num">
          {exactBytes(entry.value)}
        </span>
      ) : entry.reason ? (
        <span id={hintId} className="alk-meta" style={{ color: "var(--alkDangerText)" }}>
          {entry.reason}
        </span>
      ) : (
        <span id={hintId} />
      )}
    </Stack>
  );
}

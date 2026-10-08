// Storage sizes, in the units a person buys storage in.
//
// One rule, everywhere a byte count is read or typed: a gigabyte is
// 1,000,000,000 bytes. Drives are sold that way, plans are priced that way, and
// the storage line on an invoice is quoted that way, so a ceiling an operator
// enters as "100 GB" is 100,000,000,000 bytes and reads back as "100 GB".
//
// The alternative — storing 100 * 2^30 and dividing the display by 10^9 — is how
// the same limit came to read as "100 GB" on the admin page and "107 GB" on the
// dashboard. There is no third option that keeps both honest, so the binary
// divisor is gone from storage entirely: no "GiB" label, and no 1024 in a
// storage formatter or parser.
//
// This module is the only place either direction is spelled for the web. The
// Python twin is `alkera_core.units`; the two tables must agree, and both are
// pinned by tests that read like each other.

import { ABOVE_ZERO, NOT_PLAIN, PLAIN_NUMBER, type Entry } from "./entry";

/** The unit ladder, smallest first. Each step is 1000 of the one below it. */
const UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

export type SizeUnit = (typeof UNITS)[number];

/** Bytes in one of each unit — the multiplier an entered figure is scaled by. */
export const UNIT_BYTES: Record<SizeUnit, number> = {
  B: 1,
  KB: 1000,
  MB: 1000 ** 2,
  GB: 1000 ** 3,
  TB: 1000 ** 4,
  PB: 1000 ** 5,
};

/** The units an operator may pick when writing a ceiling. Below a gigabyte the
 *  figure is smaller than a single upload, so the picker does not offer it. */
export const LIMIT_UNITS: readonly SizeUnit[] = ["GB", "TB"];

/** The largest ceiling a route accepts: 1,000 PB, the same bound the schema
 *  names, so a field refuses what the server would. */
export const MAX_CEILING_BYTES = 1000 * UNIT_BYTES.PB;

/** What a bare number in a limit field means. Storage ceilings are written in
 *  gigabytes far more often than in anything else, so "100" is 100 GB. */
export const DEFAULT_UNIT: SizeUnit = "GB";

/** One decimal, halves away from zero — the Python twin's `_round1`. */
function round1(value: number): number {
  return Math.floor(value * 10 + 0.5) / 10;
}

/**
 * A byte count as a person reads it — `100_000_000_000` → `"100 GB"`.
 *
 * The unit is the largest one that leaves a figure of at least 1, and the figure
 * carries at most ONE decimal, halves up, with a trailing `.0` trimmed:
 * `2_500_000_000` → `"2.5 GB"`, `812_000_000` → `"812 MB"`. A figure that rounds
 * up to 1000 moves to the next unit instead of printing `"1000 MB"`, so
 * `999_999_999` reads as `"1 GB"`. Bytes never carry a decimal — they are whole
 * things.
 *
 * A null / undefined count is not zero — it is "no ceiling", which only the caller
 * can name, so this returns `null` and leaves that copy to the surface.
 */
export function formatBytes(bytes: number | null | undefined): string | null {
  if (bytes == null || !Number.isFinite(bytes)) return null;
  if (bytes < 0) return null;
  let index = 0;
  let value = bytes;
  while (index < UNITS.length - 1) {
    // Step up while the figure is at least 1000 of this unit, or still rounds
    // to it — so the reading is "1 GB" and never "1000 MB".
    if (value < 1000 && round1(value) < 1000) break;
    value /= 1000;
    index += 1;
  }
  if (index === 0) return `${Math.trunc(value)} B`;
  return `${round1(value)} ${UNITS[index]}`;
}

/** The exact byte count, grouped — the help text under an edited figure. */
export function exactBytes(bytes: number): string {
  return `${bytes.toLocaleString("en-US")} bytes`;
}

export const SIZE_TOO_LARGE = `At most ${formatBytes(MAX_CEILING_BYTES)}.`;

/** An entered figure in a picked unit, as bytes — or the reason it is not one.
 *  The text is read exactly as typed: digits and a point, nothing else, so
 *  "-4" and "1e3" are refused rather than saved as 4 and 13. Zero is refused
 *  too: it is not an edit, it is a lockout, and the editors remove a ceiling in
 *  their own words. A figure past the route's bound is refused here so the
 *  person reads the bound before saving, not after. */
export function readSize(amount: string, unit: SizeUnit): Entry {
  const trimmed = amount.trim();
  if (trimmed === "") return { value: null, reason: null };
  if (!PLAIN_NUMBER.test(trimmed)) return { value: null, reason: NOT_PLAIN };
  const bytes = Math.round(Number(trimmed) * UNIT_BYTES[unit]);
  if (bytes <= 0) return { value: null, reason: ABOVE_ZERO };
  if (bytes > MAX_CEILING_BYTES) return { value: null, reason: SIZE_TOO_LARGE };
  return { value: bytes, reason: null };
}

/** An entered figure in a picked unit, as bytes. Returns null when the text is
 *  not a positive plain size (see {@link readSize}), so a caller can keep its
 *  Save button disabled rather than writing a ceiling of NaN. */
export function bytesFrom(amount: string, unit: SizeUnit): number | null {
  return readSize(amount, unit).value;
}

const ENTRY = /^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)\s*$/;

/** Spellings a person may type for each unit. `B` admits "byte"/"bytes"; the
 *  rest admit the bare letter ("g", "t") because that is what gets typed. */
const ALIASES: Record<string, SizeUnit> = {
  b: "B",
  byte: "B",
  bytes: "B",
  k: "KB",
  kb: "KB",
  m: "MB",
  mb: "MB",
  g: "GB",
  gb: "GB",
  t: "TB",
  tb: "TB",
  p: "PB",
  pb: "PB",
};

/** A typed storage figure as bytes: `"1 TB"` → `1_000_000_000_000`. Accepts a
 *  bare number (read in `defaultUnit`), a number and a unit with or without a
 *  space, and any case. Returns null for anything that is not a positive
 *  storage size — a ceiling that cannot be read must never be silently written
 *  as something else. */
export function parseBytes(text: string, defaultUnit: SizeUnit = DEFAULT_UNIT): number | null {
  const match = ENTRY.exec(text);
  if (match == null) return null;
  const suffix = match[2].toLowerCase();
  const unit = suffix === "" ? defaultUnit : ALIASES[suffix];
  if (unit == null) return null;
  const scaled = Number(match[1]) * UNIT_BYTES[unit];
  if (!Number.isFinite(scaled) || scaled <= 0) return null;
  return Math.round(scaled);
}

/** The editor's starting point for an existing ceiling: the largest unit that
 *  divides it exactly (so "1 TB" comes back as 1 TB, not 1000 GB), falling back
 *  to GB with decimals for a figure that divides evenly into neither. */
export function splitBytes(bytes: number): { amount: string; unit: SizeUnit } {
  for (const unit of [...LIMIT_UNITS].reverse()) {
    const step = UNIT_BYTES[unit];
    if (bytes % step === 0) return { amount: String(bytes / step), unit };
  }
  const gb = bytes / UNIT_BYTES.GB;
  return { amount: String(Math.round(gb * 100) / 100), unit: "GB" };
}

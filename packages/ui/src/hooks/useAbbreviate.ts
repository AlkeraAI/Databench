import { useMemo } from "react";

/**
 * Abbreviate a number to `sig` significant digits with a K/M/B/T suffix:
 * abbreviate(9_240, 3) → "9.24K", abbreviate(439_200_000, 4) → "439.2M",
 * abbreviate(690, 3) → "690". Uses Intl compact notation so rounding and the
 * unit roll-over (999,999 → "1M") are correct and locale-aware.
 */
export function abbreviate(value: number, sig = 3): string {
  if (!Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumSignificantDigits: sig,
  }).format(value);
}

/**
 * Hook form: pass the significant-digit count once, get back a stable formatter.
 * `const fmt = useAbbreviate(3); fmt(9_240) // "9.24K"`.
 */
export function useAbbreviate(sig = 3): (value: number) => string {
  return useMemo(() => (value: number) => abbreviate(value, sig), [sig]);
}

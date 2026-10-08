// The admin console's precise money readings — the ONE place the product shows
// more than two decimals of a dollar.
//
// Everywhere else money reads through the product rule (`formatUsdNanos` in
// @alkera/chat-model: two decimals, "<$0.01" below half a cent). Alkera staff
// pricing the platform need the digits that rule rounds away: a sell price of
// $0.075 per 1M tokens, a provider cost a tenth of a cent under it, a machine
// billed $0.0083 a minute. Those readings live here, and only admin pages under
// `src/pages/platform/` may import this module — pinned by
// `src/tests/lib/format/moneyBoundary.test.ts`, so a precise figure can never
// leak onto a page an org member reads.
//
// The callers today: the model catalog's sell prices and provider costs per 1M
// tokens (`catalog/ModelCard.tsx`) and a compute grant's per-minute rate
// (`orgs/AdminOrgDetailPage.tsx`). Ledger columns in the admin console (billed,
// balances, caps, machine prices per hour) read through `usd` in `./format`, the
// product rule.

const NANO_DIGITS = 9;

/** An exact amount of `units / 10^scale` USD, every significant digit kept (never
 *  fewer than two decimals): `$0.0083`, `$15.00`, `$1,234.567891`. */
function exactUsd(units: bigint, scale: number): string {
  const negative = units < 0n;
  const digits = (negative ? -units : units).toString().padStart(scale + 1, "0");
  const whole = digits.slice(0, digits.length - scale).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = digits.slice(digits.length - scale).replace(/0+$/, "").padEnd(2, "0");
  return `${negative ? "-" : ""}$${whole}.${fraction}`;
}

/** A nano-USD amount at full precision: `8_300_000` → `$0.0083`. For a rate
 *  (per minute, per hour) an admin prices by, never for a balance. */
export function preciseUsd(nanos: number | null | undefined): string {
  if (nanos == null || !Number.isSafeInteger(nanos)) return "—";
  return exactUsd(BigInt(nanos), NANO_DIGITS);
}

/** A per-token price (nanos) expressed as USD per 1M tokens — the catalog's unit
 *  of record, exact: `per_token_nanos × 1e6 / 1e9` = dollars per million tokens,
 *  so `75` → `$0.075`. */
export function perMillionUsd(perTokenNanos: number): string {
  if (!Number.isSafeInteger(perTokenNanos)) return "—";
  return exactUsd(BigInt(perTokenNanos), NANO_DIGITS - 6);
}

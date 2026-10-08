// How the product shows a person an amount of money — ONE rule, read by every
// user-facing surface (the portal, the VS Code webview, the shared chat UI).
//
// The rule:
//   • Exactly two decimal places, grouped by thousands: `$2,999.34`.
//   • Rounded half-up (a tie goes away from zero) from the EXACT decimal value.
//     Nanos are integers and rounded as integers; a decimal string is rounded
//     digit by digit; a JS number is rounded from its shortest decimal reading
//     (`String(n)`), never from its binary value — so `1.005` reads `$1.01`,
//     where `toFixed(2)` and `Intl` would print `$1.00`.
//   • A nonzero amount that rounds to zero cents reads `<$0.01` (`-<$0.01` when
//     negative), so a real charge never reads as `$0.00`.
//   • Zero, and negative zero, read `$0.00`; a negative amount reads `-$1.23`.
//   • A value that is not a number (NaN, ±Infinity, an unparseable string)
//     reads `—`: there is no amount to show, and inventing one would be worse.
//   • A list price (a plan plate, a top-up pack, an axis tick) may drop a `.00`
//     that says nothing (`$200`); it is rounded by the same rule otherwise.
//   • A granted / used / remaining line is rounded so it adds up on screen
//     (`formatLedgerNanos`), see `ledger` below.
//
// More precision is an admin-only reading: Alkera staff pricing per token, per
// minute or per hour keep their digits through the portal's admin formatter
// (`apps/web/src/pages/platform/admin/shared/precise.ts`), which nothing
// outside the admin console may import. There is deliberately no precision
// knob here.

/** Nano-USD in one USD: the unit every money figure on the billing wire is in. */
export const NANOS_PER_USD = 1_000_000_000;

/** What a nonzero amount smaller than half a cent reads as. */
export const BELOW_ONE_CENT = "<$0.01";

/** What an amount that is not a number reads as. */
export const NO_AMOUNT = "—";

/** An exact decimal: `units / 10^scale`. */
interface Exact {
  units: bigint;
  scale: number;
}

const NANO_SCALE = 9;
const DECIMAL = /^([+-]?)(\d*)(?:\.(\d*))?(?:e([+-]?\d+))?$/i;

/** A decimal string or a finite number, read exactly; null when it is neither. */
function readDecimal(value: number | string): Exact | null {
  if (typeof value === "number" && !Number.isFinite(value)) return null;
  const text = (typeof value === "number" ? String(value) : value).trim();
  const match = DECIMAL.exec(text);
  if (!match) return null;
  const [, sign, whole = "", fraction = "", exponent = "0"] = match;
  if (whole === "" && fraction === "") return null;
  const digits = BigInt(`${whole}${fraction}` || "0");
  let scale = fraction.length - Number(exponent);
  let units = sign === "-" ? -digits : digits;
  if (scale < 0) {
    units *= 10n ** BigInt(-scale);
    scale = 0;
  }
  return { units, scale };
}

function readNanos(nanos: number | bigint | string): Exact | null {
  if (typeof nanos === "bigint") return { units: nanos, scale: NANO_SCALE };
  const exact = readDecimal(nanos);
  return exact === null ? null : { units: exact.units, scale: exact.scale + NANO_SCALE };
}

/** Whole cents, half away from zero. */
function toCents({ units, scale }: Exact): bigint {
  if (scale <= 2) return units * 10n ** BigInt(2 - scale);
  const divisor = 10n ** BigInt(scale - 2);
  const magnitude = units < 0n ? -units : units;
  const cents = (magnitude * 2n + divisor) / (divisor * 2n);
  return units < 0n ? -cents : cents;
}

function groupThousands(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** Signed cents as `$1,234.56` / `-$1,234.56`. */
function centsText(cents: bigint): string {
  const magnitude = cents < 0n ? -cents : cents;
  const dollars = groupThousands((magnitude / 100n).toString());
  const rest = (magnitude % 100n).toString().padStart(2, "0");
  return `${cents < 0n ? "-" : ""}$${dollars}.${rest}`;
}

/** The reading of an exact amount whose cents are already known. */
function reading(exact: Exact, cents: bigint): string {
  if (cents === 0n && exact.units !== 0n) return exact.units < 0n ? `-${BELOW_ONE_CENT}` : BELOW_ONE_CENT;
  return centsText(cents);
}

function format(exact: Exact | null): string {
  return exact === null ? NO_AMOUNT : reading(exact, toCents(exact));
}

/** An amount in USD — a number or a decimal string such as the wire's
 *  `"2999.339950"` — as the product shows it: `$2,999.34`. */
export function formatUsd(usd: number | string): string {
  return format(readDecimal(usd));
}

/** An amount in nano-USD (USD × 1e9, the unit the billing wire speaks) as the
 *  product shows it: `2_999_339_950_000` → `$2,999.34`. */
export function formatUsdNanos(nanos: number | bigint | string): string {
  return format(readNanos(nanos));
}

/** An amount in nano-USD as the exact plain decimal a person edits, with no
 *  trailing zeros and never an exponent: `50_000_000_000` → `"50"`,
 *  `3_500_000_000` → `"3.5"`, `1` → `"0.000000001"`. `""` when it is not a
 *  whole number of nanos. The seed for a USD field, never a display reading. */
export function nanosToUsdText(nanos: number | bigint | string): string {
  // BigInt("") is 0n: an empty or blank string is no figure, not zero.
  if (typeof nanos === "string" && nanos.trim() === "") return "";
  let units: bigint;
  try {
    units = BigInt(nanos);
  } catch {
    return "";
  }
  const sign = units < 0n ? "-" : "";
  const magnitude = units < 0n ? -units : units;
  const whole = (magnitude / BigInt(NANOS_PER_USD)).toString();
  const fraction = (magnitude % BigInt(NANOS_PER_USD)).toString().padStart(NANO_SCALE, "0").replace(/0+$/, "");
  return `${sign}${whole}${fraction ? `.${fraction}` : ""}`;
}

/** A USD amount (a decimal string or a number) as exact nano-USD, rounded half
 *  away from zero past the ninth decimal; `null` when it is not a number. */
export function usdToNanos(usd: number | string): bigint | null {
  const exact = readDecimal(usd);
  if (exact === null) return null;
  const nanos = { units: exact.units, scale: exact.scale - NANO_SCALE };
  if (nanos.scale <= 0) return nanos.units * 10n ** BigInt(-nanos.scale);
  const divisor = 10n ** BigInt(nanos.scale);
  const magnitude = nanos.units < 0n ? -nanos.units : nanos.units;
  const rounded = (magnitude * 2n + divisor) / (divisor * 2n);
  return nanos.units < 0n ? -rounded : rounded;
}

/** A list price or a round figure a person picked — a plan's monthly price, a
 *  top-up pack, a chart axis tick: whole dollars read bare (`$200`), anything
 *  else reads exactly as {@link formatUsd} does (`$12.50`). It never shows more
 *  than two decimals; it only drops a `.00` that says nothing on a price plate. */
export function formatPriceUsd(usd: number | string): string {
  const exact = readDecimal(usd);
  if (exact === null) return NO_AMOUNT;
  const cents = toCents(exact);
  const text = reading(exact, cents);
  return cents % 100n === 0n && text.endsWith(".00") ? text.slice(0, -3) : text;
}

/** One ledger line: what was granted, what was used, what remains. */
export interface LedgerFigures<T> {
  granted: T;
  used: T;
  remaining: T;
}

function alignedUnits(value: Exact, scale: number): bigint {
  return value.units * 10n ** BigInt(scale - value.scale);
}

function ledger(row: LedgerFigures<Exact | null>): LedgerFigures<string> {
  const { granted, used, remaining } = row;
  if (granted === null || used === null || remaining === null) {
    return { granted: format(granted), used: format(used), remaining: format(remaining) };
  }
  const scale = Math.max(granted.scale, used.scale, remaining.scale);
  const balances =
    alignedUnits(granted, scale) - alignedUnits(used, scale) === alignedUnits(remaining, scale);
  const grantedCents = toCents(granted);
  const remainingCents = toCents(remaining);
  // Granted and Remaining are each rounded from their exact value. When the three
  // balance exactly, Used is shown as the difference of the two figures on screen,
  // so the line still adds up to the eye: rounding each of three figures on its own
  // can leave them a cent apart (granted 1.004, used 0.005, remaining 0.999 would
  // read $1.00 − $0.01 = $1.00). Used carries the cent because Granted is what was
  // put in and Remaining is what the gateway will still spend; Used stays within a
  // cent of its exact value either way.
  const usedCents = balances ? grantedCents - remainingCents : toCents(used);
  return {
    granted: reading(granted, grantedCents),
    used: reading(used, usedCents),
    remaining: reading(remaining, remainingCents),
  };
}

/** A granted / used / remaining line in nano-USD, rounded so it adds up on screen. */
export function formatLedgerNanos(
  row: LedgerFigures<number | bigint | string>,
): LedgerFigures<string> {
  return ledger({
    granted: readNanos(row.granted),
    used: readNanos(row.used),
    remaining: readNanos(row.remaining),
  });
}

/** A granted / used / remaining line in USD, rounded so it adds up on screen. */
export function formatLedgerUsd(row: LedgerFigures<number | string>): LedgerFigures<string> {
  return ledger({
    granted: readDecimal(row.granted),
    used: readDecimal(row.used),
    remaining: readDecimal(row.remaining),
  });
}

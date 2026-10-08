// How usage, credit and budget figures are shown — ONE rule, decided here and
// read by every surface that renders them.
//
// Two postures:
//   • "percent" — the seat holds a plan allowance this cycle (opaque usage): what
//     the plan includes is shown only as a share of the cycle (or of a limit an
//     admin set), never as the dollars it is worth; the only money on screen is
//     EXTRA credit — purchased, promotional or admin-granted — when it exists.
//   • "dollars" — there is no allowance to hide, or the deployment is
//     self-hosted: every figure is money (used, remaining, funded, budgets,
//     pools), no percentages.
// On Alkera's hosted app the allowance decides, whatever the plan: a Free,
// Plus, Pro or Enterprise seat with an allowance this cycle is percent, and
// one living only on bought or granted credit is dollars. Until /me/credits
// says, the tier decides (Enterprise dollars, anything else percent), so a slow
// response can never leak a dollar figure on a self-serve plan.
// Platform staff read raw dollars regardless: they operate the platform, and
// the admin console they use is money by construction.

import { formatUsd, NANOS_PER_USD } from "@alkera/chat-model";

export type UsageDisplayMode = "percent" | "dollars";

export interface UsageDisplayInput {
  /** The org's plan tier key (`plan_tier` off /me/credits). Unknown → percent,
   *  the obfuscated posture, so a slow config can never leak a dollar figure. */
  tier: string | null | undefined;
  /** The deployment flag off the public config — the same one the self-hosted
   *  Deployment page is gated on. */
  selfHosted: boolean;
  /** The caller holds a platform role (support / admin). */
  platformStaff?: boolean;
  /** `opaque_usage` off /me/credits: the seat holds a plan allowance this
   *  cycle. Unknown (not loaded, or an older server) falls back to the tier. */
  opaqueUsage?: boolean | null;
}

export function usageDisplayMode({
  tier,
  selfHosted,
  platformStaff = false,
  opaqueUsage = null,
}: UsageDisplayInput): UsageDisplayMode {
  if (platformStaff || selfHosted) return "dollars";
  if (opaqueUsage === true) return "percent";
  if (opaqueUsage === false) return "dollars";
  return tier === "enterprise" ? "dollars" : "percent";
}

/** Display credits are thousandths of a dollar (1 credit = $0.001). */
export const CREDITS_PER_USD = 1000;

export function creditsToUsd(credits: number): number {
  return credits / CREDITS_PER_USD;
}

export function nanosToUsd(nanos: number): number {
  return nanos / NANOS_PER_USD;
}

/** "$12.40" — the product's one money reading (`formatUsd` in @alkera/chat-model):
 *  two decimals, half-up, and "<$0.01" for a real amount below half a cent. */
export function formatDollars(amountUsd: number): string {
  return formatUsd(amountUsd);
}

/** What the caller spent this cycle, whatever paid for it — the server's `cycle_used_usd`, which
 *  counts spend from an unlimited pool too. Every surface that says "used this cycle" reads this
 *  one figure. An older server without it falls back to the spend its pool rows record. */
export function cycleSpendUsd(credits: {
  cycle_used_usd?: string | null;
  limits?: readonly { used_usd: string }[] | null;
}): number {
  const own = credits.cycle_used_usd == null ? NaN : Number(credits.cycle_used_usd);
  if (Number.isFinite(own)) return own;
  return (credits.limits ?? []).reduce((sum, row) => {
    const n = Number(row.used_usd);
    return sum + (Number.isFinite(n) ? n : 0);
  }, 0);
}

/** The non-allowance balances /me/credits reports exactly. */
export interface ExtraCredits {
  prepaid_credits: number;
  promotional_credits?: number;
  on_demand_credits?: number;
  admin_cycle_credits?: number;
  admin_permanent_credits?: number;
}

/** The extra credit a seat holds, in USD — everything that is NOT the plan's
 *  cycle allotment: purchased (prepaid), promotional, on-demand, and credit an
 *  Alkera admin granted. */
export function extraCreditsUsd(credits: ExtraCredits): number {
  return creditsToUsd(
    Math.max(0, credits.prepaid_credits) +
      Math.max(0, credits.promotional_credits ?? 0) +
      Math.max(0, credits.on_demand_credits ?? 0) +
      Math.max(0, credits.admin_cycle_credits ?? 0) +
      Math.max(0, credits.admin_permanent_credits ?? 0),
  );
}

/** Each class's remaining dollars, labelled, in the order they are spent — the
 *  dollars-mode breakdown. Classes holding nothing are left out. */
export function creditsByClass(credits: ExtraCredits): { label: string; usd: number }[] {
  const rows: [string, number | undefined][] = [
    ["This cycle's granted credit", credits.admin_cycle_credits],
    ["Bonus credit", credits.promotional_credits],
    ["Granted credit", credits.admin_permanent_credits],
    ["Purchased credit", credits.prepaid_credits],
    ["On-demand credit", credits.on_demand_credits],
  ];
  return rows
    .filter((r): r is [string, number] => typeof r[1] === "number" && r[1] > 0)
    .map(([label, c]) => ({ label, usd: creditsToUsd(c) }));
}

/** "Extra credits · $12.40 left", or null when the seat holds none — the one
 *  line of money a percent-mode surface may show. */
export function extraCreditsLine(credits: ExtraCredits): string | null {
  const amount = extraCreditsUsd(credits);
  return amount > 0 ? `Extra credits · ${formatDollars(amount)} left` : null;
}

/** A whole-number percentage of `part` over `whole`, clamped to 0..100. A zero
 *  or negative `whole` reads as fully used (nothing was allotted, nothing is
 *  left) rather than a division to attempt. */
export function percentOf(part: number, whole: number): number {
  if (!(whole > 0)) return 100;
  return Math.round(Math.max(0, Math.min(1, part / whole)) * 100);
}

/** The share of a ceiling still unspent, 0..100, or null when there is no
 *  ceiling to take a share of (a zero or negative `limit`). Null is "unknown",
 *  never 0: an org whose allowance has not been recorded yet has not used it
 *  up, so the caller states the case instead of claiming nothing is left.
 *  (`percentOf` reads a zero whole the other way, as fully used — which is why
 *  the two are not one function with a flag.) */
export function percentRemaining(remaining: number, limit: number): number | null {
  if (!(limit > 0)) return null;
  return Math.round(Math.max(0, Math.min(1, remaining / limit)) * 100);
}

/** "resets in 12 d" while the boundary is ahead, "resets today" once it has arrived, null when
 *  no date is known. Days round UP so the label drops a day as the clock crosses it. */
export function resetsInLabel(iso: string | null | undefined, now: number = Date.now()): string | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  const ms = at - now;
  if (ms <= 0) return "resets today";
  return `resets in ${Math.ceil(ms / 86_400_000)} d`;
}

/** "40% remaining", or null when there is no ceiling to take a share of. */
export function percentRemainingLabel(remaining: number, limit: number): string | null {
  const share = percentRemaining(remaining, limit);
  return share == null ? null : `${share}% remaining`;
}

// What the grant route says about bytes the drive is bringing from the machine
// holding their folder. The grant's own fields come typed from the SDK; the
// refusal's detail is an untyped bag on the error and is read field by field
// and narrowed, never cast whole: a server that predates a field sends nothing,
// and a word this build has no copy for reads as absent rather than reaching a
// reader verbatim.

import type { ContentGrant } from "@/api/files";

/** What the drive's request to the machine came back with. */
export type PromoteOutcome =
  | "landed"
  | "accepted"
  | "offline"
  | "throttled"
  | "timed_out"
  | "not_holder"
  | "missing"
  | "changed"
  | "busy";

const OUTCOMES: ReadonlySet<string> = new Set<PromoteOutcome>([
  "landed",
  "accepted",
  "offline",
  "throttled",
  "timed_out",
  "not_holder",
  "missing",
  "changed",
  "busy",
]);

/** The `detail` of a `files.live_pending` refusal. */
export interface LivePendingDetail {
  /** The machine holding the bytes, by name, or null when the server named none. */
  holder: string | null;
  /** What the machine said to the request, or null when the server said nothing
   *  this build knows. */
  outcome: PromoteOutcome | null;
}

const text = (value: unknown): string | null =>
  typeof value === "string" && value !== "" ? value : null;

/** Whether a refusal is the drive saying the bytes are still on the machine. */
export function isLivePending(error: unknown): boolean {
  const status = (error as { status?: number } | null)?.status;
  const code = (error as { code?: string } | null)?.code;
  return status === 409 && code === "files.live_pending";
}

/** The refusal's detail, from anything a mint threw. Null for an error that
 *  carries no detail bag. */
export function livePendingDetail(error: unknown): LivePendingDetail | null {
  const detail = (error as { detail?: unknown } | null)?.detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return null;
  const bag = detail as Record<string, unknown>;
  const outcome = text(bag.outcome);
  return {
    holder: text(bag.holder),
    outcome: outcome !== null && OUTCOMES.has(outcome) ? (outcome as PromoteOutcome) : null,
  };
}

/** Where the bytes a grant serves stand against the machine holding them. */
export type ContentState = "on_drive" | "behind" | "unlanded" | "none";

/** Whether a grant serves an older copy than the machine holds, and when that
 *  copy reached the drive. */
export interface GrantFreshness {
  behind: boolean;
  asOf: string | null;
}

export function grantFreshness(grant: ContentGrant): GrantFreshness {
  return { behind: grant.contentState === "behind", asOf: text(grant.asOf) };
}

// The one way a connection's status is shown. The daemon and the backend each
// derive a `badge` from the three facts a connection stores (the last settled
// outcome, the check in flight, the credential's state) — the same derivation,
// `alkera_core.connections.badge.derive_badge`, on both. This module never
// derives; it turns that badge into the words and the tone every surface
// renders, so "Connected" means the same thing on the portal's team plate, the
// portal's workspace ledger, the webview's plugins page and the onboarding tour.
//
// The vocabulary is the twin of `packages/api-core/alkera_core/connections/vocab.py`;
// `status.test.ts` reads the badge fixture that pins the Python derivation and
// asserts every badge it can produce has a label here.

/** The one derived status a person sees (`alkera_core.connections.Badge`). */
export type ConnectionBadge =
  | "connected"
  | "stale"
  | "verifying"
  | "not_checked"
  | "needs_reauth"
  | "no_access"
  | "unreachable"
  | "error"
  | "incomplete"
  | "unsupported"
  | "disabled"
  | "muted";

/** What a settled verification found (`alkera_core.connections.Outcome`). */
export type ConnectionOutcome =
  | "ok"
  | "invalid_credential"
  | "permission"
  | "unreachable"
  | "timeout"
  | "unsupported"
  | "infrastructure"
  | "error";

/** What fixes a credential that needs re-authentication (`alkera_core.connections.Reauth`). */
export type ConnectionReauth = "reenter" | "browser" | "external_cli" | "admin";

export const CONNECTION_BADGES: readonly ConnectionBadge[] = [
  "connected",
  "stale",
  "verifying",
  "not_checked",
  "needs_reauth",
  "no_access",
  "unreachable",
  "error",
  "incomplete",
  "unsupported",
  "disabled",
  "muted",
];

/** How long a settled `ok` stands for before the row reads as `stale` — the twin of
 *  `FRESHNESS_HORIZON` in `alkera_core.connections.badge` (24 h). It changes the tone,
 *  never the words: both still say when the connection was last verified. The daemon and
 *  the backend derive `stale` with it; a surface that must derive from a bare wire (the
 *  workspace ledger's inventory rows) reads the same number from here. */
export const FRESHNESS_HORIZON_MS = 24 * 60 * 60 * 1000;

export type BadgeTone = "success" | "neutral" | "warning" | "danger";

/** The rendered reading of one badge. */
export interface BadgeView {
  /** The word on the pill. */
  label: string;
  tone: BadgeTone;
  /** The sentence under or beside it; empty when nothing needs saying. */
  note: string;
  /** Whether a check is out right now (a spinner beside the word). */
  busy: boolean;
}

/** The fields every connection row carries, whichever wire it came over. */
export interface BadgeSource {
  badge: ConnectionBadge | string;
  outcome?: ConnectionOutcome | string | null;
  /** The backend's or the daemon's own sentence — never driver text, never a secret. */
  badge_reason?: string | null;
  reauth?: ConnectionReauth | string | null;
  /** ISO timestamp of the last settled verification of any outcome. */
  last_verified_at?: string | null;
}

/** The label for a credential that needs a person, by what fixes it. */
export function reauthLabel(reauth: ConnectionReauth | string | null | undefined): string {
  switch (reauth) {
    case "reenter":
      return "Update credential";
    case "external_cli":
      return "Sign in with the CLI";
    case "admin":
      return "Ask your admin";
    case "browser":
    default:
      return "Sign in again";
  }
}

/** The action word for a `needs_reauth` row's menu item — the label, as a verb. */
export function reauthAction(reauth: ConnectionReauth | string | null | undefined): string {
  return reauthLabel(reauth);
}

function ladder(ms: number, now: number): string {
  const sec = Math.max(0, Math.round((now - ms) / 1000));
  if (sec < 60) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day === 1) return "a day ago";
  if (day < 7) return `${day}d ago`;
  const wk = Math.round(day / 7);
  return `${wk}w ago`;
}

/** "just now" / "12m ago" / "a day ago" from an ISO stamp; "" when there is none. */
export function verifiedAgo(iso: string | null | undefined, now: number): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  return Number.isNaN(then) ? "" : ladder(then, now);
}

/** What a row that has been looked at says about the look. One fact, the same
 *  sentence whether or not the look is old enough to have gone quiet: a reader
 *  who is told when it was last verified can judge the age themselves, and a
 *  second sentence about what has not happened since names nothing they can act
 *  on. */
function verifiedNote(ago: string): string {
  return ago ? `Verified ${ago}` : "Not verified yet";
}

/**
 * Turn a row's badge into what to render. `now` is injected so a story and a test
 * read the same words as the live page.
 */
export function presentBadge(row: BadgeSource, now: number = Date.now()): BadgeView {
  const reason = (row.badge_reason ?? "").trim();
  const ago = verifiedAgo(row.last_verified_at, now);
  switch (row.badge) {
    case "connected":
      return { label: "Connected", tone: "success", note: verifiedNote(ago), busy: false };
    case "stale":
      // The same sentence as `connected`, in the quieter tone: the look is old
      // enough to be worth noticing, and its age is the whole of what to notice.
      return { label: "Connected", tone: "neutral", note: verifiedNote(ago), busy: false };
    case "verifying":
      return { label: "Verifying…", tone: "warning", note: "", busy: true };
    case "not_checked":
      return {
        label: "Not checked",
        tone: "neutral",
        note: reason || "Nothing has checked it yet.",
        busy: false,
      };
    case "needs_reauth":
      return { label: reauthLabel(row.reauth), tone: "warning", note: reason, busy: false };
    case "no_access":
      return {
        label: "No access",
        tone: "danger",
        note: reason || "The credential works, but the account lacks permission.",
        busy: false,
      };
    case "unreachable":
      return {
        label: "Unreachable",
        tone: "danger",
        note: reason || (row.outcome === "timeout" ? "The host took too long to answer." : "The host did not answer."),
        busy: false,
      };
    case "error":
      if (row.outcome === "infrastructure") {
        return {
          label: "Couldn't check",
          tone: "warning",
          note: reason || "Couldn't run the check. Try again.",
          busy: false,
        };
      }
      return { label: "Error", tone: "danger", note: reason || "The check failed.", busy: false };
    case "incomplete":
      return { label: "Ask your admin", tone: "warning", note: reason, busy: false };
    case "unsupported":
      return { label: "Update needed", tone: "warning", note: reason, busy: false };
    case "disabled":
      return { label: "Off", tone: "neutral", note: reason, busy: false };
    case "muted":
      return { label: "Hidden from agent", tone: "neutral", note: "", busy: false };
    default:
      // A badge this build does not know: a newer server. Say so rather than
      // pretend it is healthy or broken.
      return { label: "Unknown", tone: "neutral", note: "The status is one this version doesn't recognize.", busy: false };
  }
}

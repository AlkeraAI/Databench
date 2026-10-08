// Settings vocabulary + display helpers. NOT mock data — the page reads its data from the real
// backend (api/account.ts, api/org.ts, api/dashboard.ts). This module holds settings navigation
// targets, option vocabularies for the sync selects, and display helpers for account records.

import type { LinkedIdentity, Session } from "../../../api/account";
import type { IconName } from "../../../app/icons";

export const ORG_SYNC_SECTION_ID = "org-sync";
export const ORG_SYNC_SETTINGS_TARGET = `/settings/organization#${ORG_SYNC_SECTION_ID}`;

export interface Option {
  value: string;
  label: string;
}

// OrgSyncSettings enum vocabularies — the option VALUE is the raw wire enum the API stores; the
// label is what the user reads. Pull cadence is stored as seconds, so its value is the second-count.
export const PROMOTION_OPTIONS: Option[] = [
  { value: "private", label: "Private" },
  { value: "team", label: "Team" },
  { value: "organization", label: "Organization" },
];
export const STRICTNESS_OPTIONS: Option[] = [
  { value: "standard", label: "Standard" },
  { value: "strict", label: "Strict" },
];
export const AUTO_PROMOTE_OPTIONS: Option[] = [
  { value: "human_or_self_verify", label: "Human or self-verify" },
  { value: "human_only", label: "Human only" },
  { value: "off", label: "Off" },
];
export const CADENCE_OPTIONS: Option[] = [
  { value: "60", label: "1 minute" },
  { value: "300", label: "5 minutes" },
  { value: "900", label: "15 minutes" },
  { value: "3600", label: "1 hour" },
];

/** The label for a given enum value, falling back to the raw value if it's unknown to us. */
export function labelFor(options: Option[], value: string): string {
  return options.find((o) => o.value === value)?.label ?? value;
}

// The OAuth providers the product supports, in display order. The identities endpoint returns only
// the LINKED ones, so the page intersects this list with what came back to render connect/connected.
export const PROVIDERS: { key: string; label: string }[] = [
  { key: "google", label: "Google" },
  { key: "github", label: "GitHub" },
];

/** Find the linked identity for a provider key, or null when the caller hasn't connected it. */
export function identityFor(identities: LinkedIdentity[], key: string): LinkedIdentity | null {
  return identities.find((i) => i.provider.toLowerCase() === key) ?? null;
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31536000],
  ["month", 2592000],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
];

/** A compact relative time ("2 hours ago", "in 6 days") for a session timestamp. `now` is injectable
 *  so a test can pin it. */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const diffSec = Math.round((new Date(iso).getTime() - now) / 1000);
  const abs = Math.abs(diffSec);
  if (abs < 45) return "just now";
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  for (const [unit, secs] of UNITS) {
    if (abs >= secs) return rtf.format(Math.round(diffSec / secs), unit);
  }
  return rtf.format(Math.round(diffSec / 60), "minute");
}

export interface ParsedAgent {
  browser: string | null;
  os: string | null;
  mobile: boolean;
}

// Ordered longest-lived-lie first: every Chromium browser claims "Chrome" and "Safari" in its UA,
// and Safari claims neither, so a naive scan reports Edge and Opera as Chrome and Chrome as Safari.
// Each entry's pattern is what distinguishes it from everything BELOW it in this list.
const BROWSERS: [RegExp, string][] = [
  [/\bEdgA?\//, "Edge"],
  [/\bOPR\/|\bOpera\//, "Opera"],
  [/\bSamsungBrowser\//, "Samsung Internet"],
  [/\bFxiOS\/|\bFirefox\//, "Firefox"],
  [/\bCriOS\//, "Chrome"],
  [/\bChrome\//, "Chrome"],
  [/\bSafari\//, "Safari"],
  [/\bcurl\//, "curl"],
];

const OSES: [RegExp, string][] = [
  [/\bWindows NT\b/, "Windows"],
  [/\b(iPhone|iPad|iPod)\b/, "iOS"],
  [/\bMac OS X\b|\bMacintosh\b/, "macOS"],
  [/\bAndroid\b/, "Android"],
  [/\bCrOS\b/, "ChromeOS"],
  [/\bLinux\b/, "Linux"],
];

/** The browser + OS a user agent names, as much as it names. Deliberately small: enough to tell
 *  "Chrome on macOS" from "Safari on iOS" when deciding which session to revoke, and nothing that
 *  would need a third-party UA database to keep honest. Unknown agents report nulls rather than a
 *  guess — a wrong device name is worse than none on a security surface. */
export function parseUserAgent(ua: string | null | undefined): ParsedAgent {
  if (!ua) return { browser: null, os: null, mobile: false };
  const browser = BROWSERS.find(([re]) => re.test(ua))?.[1] ?? null;
  const os = OSES.find(([re]) => re.test(ua))?.[1] ?? null;
  return { browser, os, mobile: /\b(iPhone|iPad|iPod|Android|Mobile)\b/.test(ua) };
}

/** The two halves of the API's `client` string: the user agent and the coarse network prefix the
 *  session was last seen from, joined server-side by " · ". */
export function sessionClient(s: Session): { userAgent: string | null; network: string | null } {
  const parts = (s.client ?? "").split("·").map((p) => p.trim());
  const network = parts.find((p) => /^[0-9a-f.:]+\/\d+$/i.test(p)) ?? null;
  const userAgent = parts.find((p) => p && p !== network) ?? null;
  return { userAgent, network };
}

/** The row title for a session: its server label, else the device the user agent names, else the
 *  token type. A browser session carries no label, so the user agent is what makes one row
 *  distinguishable from the next — without it every row reads "Browser session". */
export function sessionTitle(s: Session): string {
  if (s.label) return s.label;
  if (s.token_type === "cli") return "CLI token";
  const { browser, os } = parseUserAgent(sessionClient(s).userAgent);
  if (browser && os) return `${browser} on ${os}`;
  return browser ?? os ?? "Browser session";
}

/** The sub-line under a session: where it was last seen, and how recently. Each fact about a
 *  session is stated once across this line and `sessionTimes` — the expiry and the sign-in are
 *  moments, so they live on the other line. */
export function sessionMeta(s: Session, now: number = Date.now()): string {
  const used = s.last_used_at ? `Last used ${relativeTime(s.last_used_at, now)}` : "Not used yet";
  const network = sessionClient(s).network;
  return [network, used].filter(Boolean).join(" · ");
}

/** The exact timestamps behind the relative sub-line. Relative times answer "is this recent?";
 *  deciding whether a session is yours needs the real moment, so this is rendered as text on the
 *  row — a hover `title` is not reachable by keyboard and is unreliable for screen readers, which
 *  would have put the one fact added for "is this mine?" out of reach of the people most likely
 *  to need it. */
export function sessionTimes(s: Session): string {
  const at = (iso: string) => new Date(iso).toLocaleString();
  return `Signed in ${at(s.issued_at)} · Expires ${at(s.expires_at)}`;
}

/** The accessible name for a row's Revoke button. Every row's button reads "Revoke" on screen, so
 *  without this a screen reader is offered a list of identical controls — exactly the ambiguity
 *  this surface exists to remove. It names the device, the coarse network (what tells two
 *  sessions on the same browser apart) and the absolute moments rather than the relative ones:
 *  the control is where the decision is taken. */
export function sessionRevokeLabel(s: Session): string {
  const network = sessionClient(s).network;
  return [`Revoke ${sessionTitle(s)}`, network, sessionTimes(s)].filter(Boolean).join(", ");
}

/** The device glyph for a session row — CLI tokens get the code mark, phones a device, the rest a
 *  monitor. Read from the user agent, since a browser session has no label to read. */
export function sessionIcon(s: Session): IconName {
  if (s.token_type === "cli") return "code";
  if (/ios|iphone|ipad|android|mobile/i.test(s.label ?? "")) return "device";
  return parseUserAgent(sessionClient(s).userAgent).mobile ? "device" : "monitor";
}

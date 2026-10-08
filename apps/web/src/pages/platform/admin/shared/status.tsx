// The admin registry's status vocabulary, as data-driven Pills. A status (a token's
// active/expired/revoked grade, a provider's pigment, a platform role)
// maps to a {tone, label, icon?} record, and one small component renders it — a
// registry, not an if/elif chain, so a new status is a row not a branch. Every
// status carries an icon or a dot so it never rides hue alone.

import type { ReactNode } from "react";

import type { components } from "@alkera/sdk";
import { Pill, resolveProviderMark, type PillTone } from "@alkera/ui";

import styles from "./status.module.css";
import { Icon, type IconName } from "../../../../app/icons";
import type { PlatformRole } from "../../../../api/admin/admin";

type Provider = components["schemas"]["Provider"];

// ---- proxy-token grade ----------------------------------------------------

export type TokenGrade = "active" | "expired" | "revoked";

/** Resolve a proxy token's grade from its timestamps. Revoked wins; an elapsed
 *  expiry is "expired"; otherwise "active". */
export function tokenGrade(token: { revoked_at: string | null; expires_at: string | null }): TokenGrade {
  if (token.revoked_at) return "revoked";
  if (token.expires_at && new Date(token.expires_at).getTime() < Date.now()) return "expired";
  return "active";
}

const TOKEN_GRADE: Record<TokenGrade, { tone: PillTone; label: string }> = {
  active: { tone: "success", label: "Active" },
  expired: { tone: "neutral", label: "Expired" },
  revoked: { tone: "danger", label: "Revoked" },
};

/** A proxy token's status as a rect grade chip with a status dot. */
export function TokenGradePill({ grade }: { grade: TokenGrade }) {
  const g = TOKEN_GRADE[grade];
  return (
    <Pill tone={g.tone} shape="rect" dot>
      {g.label}
    </Pill>
  );
}

// ---- provider pigment -----------------------------------------------------

// The three providers ride the earthy categorical tones so they read as one warm pigment family,
// never cold neon — anthropic olive-green, bedrock green, openai amber (cat5's blue was the one cold
// leak the brand forbids). Stable assignment so a provider keeps its colour.
const PROVIDER_TONE: Record<Provider, PillTone> = {
  anthropic: "cat1",
  bedrock: "cat3",
  openai: "cat2",
};

// Providers with a real brand mark carry it; bedrock has none, so it falls back to the layered-infra
// bench glyph (never bare, so the provider always reads by a mark not hue alone).
const PROVIDER_FALLBACK_ICON: Record<Provider, IconName> = {
  anthropic: "flask",
  bedrock: "layers",
  openai: "circleRing",
};

/** A provider as a soft categorical pill, led by its brand logo (anthropic / openai) or a bench glyph. */
export function ProviderPill({ provider }: { provider: Provider }) {
  const mark = resolveProviderMark(provider, { size: 13 });
  return (
    <Pill tone={PROVIDER_TONE[provider]} icon={mark ?? <Icon name={PROVIDER_FALLBACK_ICON[provider]} size={12} />}>
      {provider}
    </Pill>
  );
}

// ---- platform role --------------------------------------------------------

const PLATFORM_ROLE: Record<PlatformRole, { tone: PillTone; label: string }> = {
  alkera_admin: { tone: "brand", label: "Platform admin" },
  alkera_support: { tone: "info", label: "Platform support" },
};

/** A platform role as a rect grade chip with the shield glyph. A regular user (no
 *  role) renders the muted dash via the caller, not here. */
export function PlatformRolePill({ role, display }: { role: PlatformRole; display?: string | null }) {
  const r = PLATFORM_ROLE[role];
  return (
    <Pill tone={r.tone} shape="rect" icon={<Icon name="shield" size={12} />}>
      {display || r.label}
    </Pill>
  );
}

// ---- enabled / disabled ---------------------------------------------------

/** An on/off grade chip for a model or a route — success when on, muted when off,
 *  each with a dot so the state doesn't ride colour alone. */
export function EnabledPill({ on, onLabel = "Enabled", offLabel = "Disabled" }: { on: boolean; onLabel?: string; offLabel?: string }) {
  return (
    <Pill tone={on ? "success" : "neutral"} shape="rect" dot>
      {on ? onLabel : offLabel}
    </Pill>
  );
}

/** Email-verification state — the abuse-monitoring column: an account is
 *  warning-toned until its address is proven. */
export function VerifiedPill({ verifiedAt }: { verifiedAt: string | null | undefined }) {
  return (
    <Pill tone={verifiedAt ? "success" : "warning"} shape="rect" dot>
      {verifiedAt ? "Verified" : "Unverified"}
    </Pill>
  );
}

/** Struck on an account the platform has banned. The row stays in the register
 *  (a reader still has to find the account) but this is the state that overrides
 *  every other: a barred glyph so it never rides hue alone, and the recorded
 *  reason on hover. */
export function BannedPill({ reason }: { reason?: string | null }) {
  return (
    <Pill tone="danger" shape="rect" icon={<Icon name="triBarred" size={12} />} title={reason || undefined}>
      Banned
    </Pill>
  );
}

/** Struck on accounts whose email domain is a known disposable-mail provider —
 *  the strongest single spam-account signal the register carries. */
export function DisposableSeal() {
  return (
    <Pill tone="danger" shape="rect" variant="outline">
      Disposable
    </Pill>
  );
}

// ---- the assay-fail seal --------------------------------------------------

/** The signature mark: a "Below cost" rejection seal, struck when a model's sell
 *  price sits under its provider cost. Danger pigment + a barred-triangle glyph,
 *  so it never relies on hue. The caller decides when to show it (margin < 0). */
export function BelowCostSeal({ children = "Below cost", icon }: { children?: ReactNode; icon?: ReactNode }) {
  return (
    <Pill tone="danger" shape="rect" variant="outline" icon={icon} className={styles.seal}>
      {children}
    </Pill>
  );
}

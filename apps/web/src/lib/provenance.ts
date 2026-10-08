import type { CSSProperties } from "react";
import type { TrustGrade } from "@alkera/chat-model";

export { toTrustGrade, type TrustGrade } from "@alkera/chat-model";

/** Provenance vocabulary shared across the portal's concern pages. `Origin` is a fact or node's
 *  descent — where it came from; `TrustGrade` is its assay verdict. Both render first-class
 *  (a pigment, an assay mark), never buried in a tooltip. */

// `synced` is a LEGACY descent. A pull carries each item's true authoring origin through
// unchanged — a teammate's note stays `human` — so nothing stamps `synced` any more. It stays
// in the vocabulary because older rows still carry it, and for those rows the label is right.
// Whether an entry reached Alkera is `Fact.syncState`; who shared it is `owner` / `isMine`.
export type Origin = "human" | "agent" | "connector" | "seed" | "synced";

export const ORIGIN_LABEL: Record<Origin, string> = {
  human: "Human",
  agent: "Agent",
  connector: "Connector",
  seed: "Seed",
  synced: "Synced",
};

/** What each descent means, in the sentence a reader gets on the mark itself. */
export const ORIGIN_NOTE: Record<Origin, string> = {
  human: "A person on the team wrote it.",
  agent: "The agent wrote it from its own work.",
  connector: "A plugin brought it in from a connected source.",
  seed: "Filed when the workspace first connected.",
  synced: "It arrived from the shared catalog.",
};

const KNOWN_ORIGINS: readonly Origin[] = ["human", "agent", "connector", "seed", "synced"];

/** Normalize a wire origin. Unknown values read as `agent`, so a newer writer's origin never
 *  renders as a person's word. Every data source (cloud KbItem, daemon ContextItem, lineage
 *  rows) normalizes through here, so one vocabulary reaches the page. */
export function normOrigin(value: string): Origin {
  return (KNOWN_ORIGINS as readonly string[]).includes(value) ? (value as Origin) : "agent";
}

export const TRUST_LABEL: Record<TrustGrade, string> = {
  verified: "Verified",
  agent: "Agent-set",
  unverified: "Unverified",
};

/** The warm provenance pigment for each origin (a domain palette, not a ui tone). */
const ORIGIN_PIGMENT: Record<Origin, string> = {
  human: "var(--alkPigHuman)",
  agent: "var(--alkPigAgent)",
  connector: "var(--alkPigConnector)",
  seed: "var(--alkPigSeed)",
  synced: "var(--alkPigSynced)",
};

/** The origin's own pigment, for a mark that is the legend for a row's origin dot. */
export function originPigment(origin: Origin): string {
  return ORIGIN_PIGMENT[origin];
}

/** Drive a `<Pill dot>`'s dot colour with the origin's pigment. The origin chip is just
 *  `<Pill tone="neutral" dot style={originDotStyle(o)}>{ORIGIN_LABEL[o]}</Pill>`. */
export function originDotStyle(origin: Origin): CSSProperties {
  return { ["--alk-pill-dot" as string]: ORIGIN_PIGMENT[origin] } as CSSProperties;
}

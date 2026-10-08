// The generated subagent identity: one hash, one name table, shared by every
// surface that names or draws a spawned agent. The hash keys avatar pixels and
// generated names for seeds persisted in on-disk transcripts, so its constants
// and the table's order are a compatibility contract — changing either renames
// every replayed agent.

import type { ConversationPart } from "./conversation";

const SUBAGENT_NAMES = [
  "Noether",
  "Curie",
  "Lovelace",
  "Franklin",
  "Turing",
  "Hopper",
  "Meitner",
  "Dirac",
  "Feynman",
  "Leibniz",
  "Hypatia",
  "Bohr",
];

/** FNV-1a over the seed, folded positive — the shared avatar/name hash. */
export function hashPixelAvatarSeed(seed: string): number {
  let hash = 2166136261;
  for (let index = 0; index < seed.length; index += 1) {
    hash ^= seed.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return Math.abs(hash);
}

/** Deterministic generated agent name for a seed. */
export function generatedSubagentName(seed: string): string {
  return SUBAGENT_NAMES[hashPixelAvatarSeed(seed) % SUBAGENT_NAMES.length];
}

/** Return Alkera-generated subagent names instead of raw provider labels. */
export function subagentDisplayName(part: Extract<ConversationPart, { kind: "subagent" }>): string {
  return generatedSubagentName(part.avatarSeed ?? part.childSessionId ?? part.name);
}

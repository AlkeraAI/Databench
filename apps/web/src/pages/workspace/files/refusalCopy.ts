/**
 * The one mapping from a capability refusal to the sentence a person reads.
 *
 * An item's `capabilities.refusals` carries, per action, WHY it is refused — as
 * a reason code (`insufficient_role`, `held`, `no_reshare`, …), not as copy.
 * Every surface that shows why a control is disabled asks here, so a reason
 * code never reaches a person and two screens never word the same refusal
 * differently. A reason that is already a sentence is kept as the server wrote
 * it; a code with no row falls back to the capability's own plain sentence.
 */

import type { Item } from "@/api/files";

export type CapName =
  | "can_read"
  | "can_write"
  | "can_share"
  | "can_delete"
  | "can_rename"
  | "can_download";

/** The plain sentence per capability, for a refusal that names no more. */
export const CAP_REFUSAL: Readonly<Record<CapName, string>> = {
  can_read: "You do not have access to this item.",
  can_write: "You cannot change this item.",
  can_share: "You cannot share this item.",
  can_delete: "You cannot delete this item.",
  can_rename: "You cannot rename this item.",
  can_download: "You cannot download this item.",
};

/** What the refused action is called in "You can view this, not … it." */
const CAP_VERB: Readonly<Record<CapName, string>> = {
  can_read: "open",
  can_write: "change",
  can_share: "share",
  can_delete: "delete",
  can_rename: "rename",
  can_download: "download",
};

/** The key the server files a capability's refusal under — the action it
 *  decides, which is not always the capability's own name. */
const REFUSAL_KEY: Readonly<Record<CapName, readonly string[]>> = {
  can_read: ["read"],
  can_write: ["write"],
  can_share: ["share"],
  can_delete: ["delete"],
  can_rename: ["rename", "write"],
  can_download: ["export", "download"],
};

/** A reason that is a state of the item, not the caller's rung. */
const STATE_REFUSAL: Readonly<Record<string, string>> = {
  held: "This is on legal hold.",
  frozen: "This drive is over its storage limit.",
  locked: "This is locked.",
  leased: "Someone has this folder for local use.",
  no_download: "Downloads are turned off for this item.",
  no_reshare: "Resharing is turned off for this item.",
};

/** The rung the caller holds on the item, read off what it may do: the
 *  capabilities are the only role fact the wire carries. */
function rung(item: Item): "view" | "edit" | null {
  const caps = item.capabilities;
  if (caps?.can_write === true) return "edit";
  if (caps?.can_read === true) return "view";
  return null;
}

/** A reason is copy already when it reads as a sentence, not as a code. */
function isSentence(reason: string): boolean {
  return /\s/.test(reason.trim());
}

/**
 * Why `item` refuses `cap`, as a sentence, or `undefined` when it does not.
 *
 * @param verb the word for what was refused, where the control says more than
 *   the capability does (Move to… is refused by `can_write`, and "not move it"
 *   reads better than "not change it").
 */
export function capabilityRefusal(item: Item, cap: CapName, verb?: string): string | undefined {
  const caps = item.capabilities;
  if (caps?.[cap] === true) return undefined;
  const refusals: Readonly<Record<string, string>> = caps?.refusals ?? {};
  const reason = [...REFUSAL_KEY[cap], cap].map((key) => refusals[key]).find(Boolean);
  return refusalSentence(item, cap, reason, verb);
}

/** The sentence for one reason code on one item. */
export function refusalSentence(
  item: Item,
  cap: CapName,
  reason: string | undefined,
  verb?: string,
): string {
  if (!reason) return CAP_REFUSAL[cap];
  if (isSentence(reason)) return reason;
  const code = reason.startsWith("files.") ? reason.slice("files.".length) : reason;
  const state = STATE_REFUSAL[code];
  if (state) return state;
  if (code === "insufficient_role") {
    const held = rung(item);
    if (held) return `You can ${held} this, not ${verb ?? CAP_VERB[cap]} it.`;
  }
  return CAP_REFUSAL[cap];
}

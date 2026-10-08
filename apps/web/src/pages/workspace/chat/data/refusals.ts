// Failed-turn causes an extension names. The fold classifies a failure by the
// refusal code the machine persisted, else by the raw wire sentence; the open
// chat knows provider, model, context and process failures, and an extension
// that refuses turns for reasons of its own registers its codes and sentences
// here. With nothing registered such a failure falls to the open rules, and to
// the raw sentence when none of them matches.

import { ExtensionPoint } from "@alkera/ui/extensions";

/** The facts a refusal rides in with, as the machine persisted them. `fields` is
 *  the whole persisted refusal, for the extension that owns the code. */
export interface RefusalFacts {
  code: string;
  teamName?: string | null;
  resetsAt?: string | null;
  /** The page that fixes it, present only when the wire decided this reader may open it. */
  manageUrl?: string | null;
  fields?: unknown;
}

/** A refusal code an extension owns: the cause the surface answers, the sentence the
 *  reader sees, and an optional second line. */
export interface NamedRefusal {
  /** The refusal code. */
  key: string;
  cause: string;
  title: (facts: RefusalFacts) => string;
  detail?: (facts: RefusalFacts) => string | null;
}

export const NAMED_REFUSALS = new ExtensionPoint<NamedRefusal>("chat.named_refusals");

/** A raw wire sentence an extension recognises. Matched before the open rules, so a
 *  sentence that also carries words of a transport failure is claimed by its owner. */
export interface FailureSentence {
  key: string;
  cause: string;
  match: RegExp;
  title: string;
}

export const FAILURE_SENTENCES = new ExtensionPoint<FailureSentence>("chat.failure_sentences");

export function namedRefusal(code: string): NamedRefusal | undefined {
  return NAMED_REFUSALS.items().find((entry) => entry.key === code);
}

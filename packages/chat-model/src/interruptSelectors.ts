// Transcript selectors for the interactive interrupts: the question a surface
// should raise and the permissions still awaiting an answer. Both frontends
// read these over the same turn model, so they live with it.

import type { ConversationTurn, PermissionConversationPart, QuestionConversationPart } from "./conversation";
import type { AskCall } from "./permissionPresentation";

export function findActiveQuestion(turns: ConversationTurn[]): QuestionConversationPart | undefined {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const turn = turns[i];
    for (let j = turn.parts.length - 1; j >= 0; j -= 1) {
      const part = turn.parts[j];
      if (part.kind === "question" && part.status === "pending") return part;
    }
  }
  return undefined;
}

export function questionGalleryKey(part: QuestionConversationPart): string {
  const shape = part.questions
    .map((question) => `${question.question}:${question.options.map((option) => option.label).join(",")}`)
    .join("|");
  return `${part.requestId}:${shape}`;
}

/** The tool call a permission ask gates, when the transcript holds it: its
 *  name and input, which the presentation shows for an ask that named nothing
 *  else. Matched by either id the ask gave for the call. */
export function findGatedCall(
  turns: ConversationTurn[],
  ask: PermissionConversationPart,
): AskCall | undefined {
  const ids = new Set(ask.callIds ?? []);
  if (ids.size === 0) return undefined;
  for (const turn of turns) {
    for (const part of turn.parts) {
      if (part.kind === "tool" && ids.has(part.callId)) {
        return { name: part.name, input: part.input ?? {} };
      }
    }
  }
  return undefined;
}

export function findPendingPermissions(turns: ConversationTurn[]): PermissionConversationPart[] {
  const pending: PermissionConversationPart[] = [];
  for (const turn of turns) {
    for (const part of turn.parts) {
      if (part.kind === "permission" && part.status === "pending" && part.prompting) pending.push(part);
    }
  }
  return pending;
}

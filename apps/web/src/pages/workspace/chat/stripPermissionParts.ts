// Pure transcript-shaping helpers: which parts render at all, and the plain-text
// fallback for the system divider. No JSX.

import type { ConversationPart, ConversationTurn } from "@alkera/chat-model";

/** Remove interrupt-only and invisible parts before transcript history renders.
 *  An ask stays out while it is open (it is an interrupt, not history) and
 *  comes back once the log records the option it was settled on — by this
 *  reader, another viewer or the policy — so the decision is on the tape. */
export function stripPermissionParts(turns: ConversationTurn[]): ConversationTurn[] {
  return turns
    .map((turn) => {
      const parts = turn.parts.filter((part) => isDecidedAsk(part) || (part.kind !== "permission" && isVisiblePart(part)));
      return parts.length === turn.parts.length ? turn : { ...turn, parts };
    })
    .filter((turn) => turn.parts.length > 0);
}

/** An ask the log settled on a named option. One settled without an option
 *  (the machine stopped waiting) records no decision, so there is nothing to show. */
export function isDecidedAsk(part: ConversationPart): boolean {
  return part.kind === "permission" && part.status === "resolved" && Boolean(part.selectedOptionId);
}

function isVisiblePart(part: ConversationPart): boolean {
  if (part.kind === "text" || part.kind === "thinking" || part.kind === "system") {
    return part.text.trim().length > 0;
  }
  if (part.kind === "tool") return true;
  if (part.kind === "question") return part.questions.length > 0;
  if (part.kind === "file_edited" || part.kind === "subagent" || part.kind === "permission") return true;
  if (part.kind === "command") return true;
  if (part.kind === "compaction") {
    return Boolean(part.streaming || part.title?.trim() || part.text.trim());
  }
  if (part.kind === "plan") return part.entries.length > 0;
  if (part.kind === "turn_summary") {
    return Boolean(
      part.summary
      || part.stopReason
      || part.costUsd != null
      || part.tokens
      || part.files.length
      || part.importantFiles?.length
      || part.artifacts?.length
      || part.graphNodes?.length
      || part.runs?.length
      || part.errorDetail,
    );
  }
  return false;
}

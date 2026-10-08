import { EmptyState, PlanTextPanel, StackedPage } from "@alkera/ui";
import type { ConversationTurn, QuestionConversationPart } from "@alkera/chat-model";
import { useCallback, useMemo } from "react";
import { useParams } from "react-router-dom";
import { useStackedBack } from "./crumbTrail";
import { StackedCrumbs } from "./StackedCrumbs";
import { useTranscriptLookup } from "./useTranscriptLookup";

/** The plan fallback page for hosts, such as the browser preview, that cannot
 *  open the editor document owned by chat/planDocument.ts. */
export function PlanSurface() {
  return (
    <PlanBody />
  );
}

function PlanBody() {
  const { chatId, partId } = useParams();
  const back = useStackedBack();
  // A plan the reader followed a link to can sit above the page the chat opened
  // on; the read pages up until it holds the card.
  const until = useCallback(
    (turns: ConversationTurn[]) => findPlanPart(turns, partId) !== null,
    [partId],
  );
  const lookup = useTranscriptLookup(chatId, { until });
  const plan = useMemo(() => findPlanPart(lookup.turns, partId), [partId, lookup.turns]);
  const planText = plan?.planMarkdown || plan?.questions[0]?.question || "";

  return (
    <StackedPage nav={<StackedCrumbs current="Plan" />} onBack={back}>
      {lookup.loading || lookup.searching ? (
        <div role="status">Loading plan…</div>
      ) : planText ? (
        <PlanTextPanel plan={planText} />
      ) : (
        <EmptyState size="md" role="alert" title="Plan not found." />
      )}
    </StackedPage>
  );
}

function findPlanPart(
  turns: ConversationTurn[],
  partId: string | undefined,
): QuestionConversationPart | null {
  if (!partId) return null;
  for (const turn of turns) {
    const part = turn.parts.find(
      (candidate) => candidate.kind === "question" && candidate.id === partId && candidate.questionKind === "plan_approval",
    );
    if (part?.kind === "question") return part;
  }
  return null;
}

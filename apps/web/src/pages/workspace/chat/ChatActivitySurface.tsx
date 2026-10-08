import { ChatActivity, StackedPage } from "@alkera/ui";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { activityKeys } from "./chatKeys";
import { useStackedBack } from "./crumbTrail";
import { StackedCrumbs } from "./StackedCrumbs";
import { chatData, errorText } from "./data";

/**
 * The per-chat activity page: a Decisions | Cost | Safety segmented view.
 * Decisions + Safety come from the permission-decision audit log (Safety = the
 * `decided_by="judge"` subset); Cost shows the spent-vs-caps gauge, an editable
 * caps form, and the warehouse cost ledger.
 */
export function ChatActivitySurface() {
  return (
    <ChatActivityBody />
  );
}

function ChatActivityBody() {
  const { chatId } = useParams();
  const queryClient = useQueryClient();
  const id = chatId ?? "";
  const back = useStackedBack();

  const decisions = useQuery({
    queryKey: activityKeys.decisions(id),
    enabled: Boolean(id),
    queryFn: () => chatData().listDecisions(id),
  });
  const safety = useQuery({
    queryKey: activityKeys.safety(id),
    enabled: Boolean(id),
    queryFn: () => chatData().listDecisions(id, { decidedBy: "judge" }),
  });
  const cost = useQuery({
    queryKey: activityKeys.cost(id),
    enabled: Boolean(id),
    queryFn: () => chatData().getCostState(id),
  });
  const ledger = useQuery({
    queryKey: activityKeys.ledger(id),
    enabled: Boolean(id),
    queryFn: () => chatData().listCostLedger(id),
  });

  return (
    <StackedPage nav={<StackedCrumbs current="Chat activity" />} onBack={back}>
      <ChatActivity
        decisions={{ data: decisions.data?.decisions, loading: decisions.isPending, error: queryError(decisions) }}
        safety={{ data: safety.data?.decisions, loading: safety.isPending, error: queryError(safety) }}
        cost={{ data: cost.data, loading: cost.isPending, error: queryError(cost) }}
        ledger={{ data: ledger.data?.entries, loading: ledger.isPending, error: queryError(ledger) }}
        onSaveCaps={async (caps) => {
          const next = await chatData().setCostLimits(id, caps);
          // Reflect the refreshed gauge immediately, then revalidate the ledger.
          queryClient.setQueryData(activityKeys.cost(id), next);
          void queryClient.invalidateQueries({ queryKey: activityKeys.ledger(id) });
        }}
      />
    </StackedPage>
  );
}

function queryError(query: { isError: boolean; error: unknown }): string | null {
  return query.isError ? errorText(query.error) : null;
}

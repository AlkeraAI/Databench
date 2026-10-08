import { CompactionTextPanel, EmptyState, StackedPage } from "@alkera/ui";
import type { CompactionConversationPart, ConversationTurn } from "@alkera/chat-model";
import { useCallback, useMemo } from "react";
import { useParams } from "react-router-dom";
import { useStackedBack } from "./crumbTrail";
import { StackedCrumbs } from "./StackedCrumbs";
import { useTranscriptLookup } from "./useTranscriptLookup";

export function CompactionSurface() {
  return (
    <CompactionBody />
  );
}

function CompactionBody() {
  const { chatId, partId } = useParams();
  const back = useStackedBack();
  // The link can name a compaction many pages above the tail the chat opens on,
  // so the read walks up until it holds the card rather than reporting the
  // window's own edge as "not found".
  const until = useCallback(
    (turns: ConversationTurn[]) => findCompactionPart(turns, partId) !== null,
    [partId],
  );
  const lookup = useTranscriptLookup(chatId, { until });
  const compaction = useMemo(
    () => findCompactionPart(lookup.turns, partId),
    [partId, lookup.turns],
  );
  const title = compaction?.title?.trim() || "Compaction";

  return (
    <StackedPage nav={<StackedCrumbs current={title} />} onBack={back}>
      {compaction ? (
        <CompactionTextPanel text={compaction.text} />
      ) : lookup.loading || lookup.searching ? (
        // Still reading. A transcript being paged has not said the summary is
        // absent, and announcing it missing while the read is in flight is how
        // a slow page read as a broken one.
        <div role="status">Loading compaction…</div>
      ) : lookup.error ? (
        <EmptyState size="md" role="alert" title="This chat could not be read." />
      ) : (
        <EmptyState size="md" role="alert" title="Compaction not found." />
      )}
    </StackedPage>
  );
}

function findCompactionPart(
  turns: ConversationTurn[],
  partId: string | undefined,
): CompactionConversationPart | null {
  if (!partId) return null;
  for (const turn of turns) {
    const part = turn.parts.find((candidate) => candidate.kind === "compaction" && candidate.id === partId);
    if (part?.kind === "compaction") return part;
  }
  return null;
}

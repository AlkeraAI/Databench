// A transcript read that does not stop at the window.
//
// A chat opens on its newest page and reads the pages above it as the reader
// scrolls up, so a surface that has to FIND
// something in the transcript can no longer assume the turns it is handed are
// all of it: the compaction a link names, the plan a card points at, the spawn
// card that gave a subagent its name and every large result a chat produced can
// all sit pages above the tail. Each of those reads pages up from the durable
// record until what it is after is loaded, or the transcript runs out — which
// is what keeps a link into a week-old turn resolving instead of answering
// "not found" because the window happens to start below it.
//
// A source that serves whole transcripts reports nothing older, so the lookup
// is the plain read it always was.

import type { ConversationTurn } from "@alkera/chat-model";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { chatKeys } from "./chatKeys";
import { chatData } from "./data";

/** How many pages one lookup reads before it stops asking. A deep link into a
 *  chat with days of history is worth a few round trips; walking a hundred
 *  thousand events to decide a breadcrumb's label is not, and a lookup that
 *  cannot be satisfied would otherwise read the whole transcript every time the
 *  surface re-renders. */
export const LOOKUP_PAGE_BUDGET = 20;

export interface TranscriptLookup {
  /** Every turn loaded so far, oldest first. */
  turns: ConversationTurn[];
  /** The first read has not come back yet. */
  loading: boolean;
  /** Older pages are still being read for what this lookup is after — the
   *  caller shows its "loading" state rather than "not found". */
  searching: boolean;
  /** The read itself failed. A chat that could not be READ has not said the
   *  thing is absent, and a surface says so in different words. */
  error: boolean;
}

/**
 * A chat's turns, paged upward until `until` is satisfied.
 *
 * Without `until` the lookup is exhaustive: it reads until the transcript has
 * no more pages (or the budget runs out), which is what a surface listing
 * everything a chat produced needs. With one, it stops the moment the thing it
 * is after is loaded, so a link into the most recent turn costs no extra read.
 */
export function useTranscriptLookup(
  chatId: string | null | undefined,
  opts: { enabled?: boolean; until?: (turns: ConversationTurn[]) => boolean } = {},
): TranscriptLookup {
  const enabled = (opts.enabled ?? true) && Boolean(chatId);
  const until = opts.until;
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: chatKeys.turns(chatId),
    enabled,
    queryFn: () => chatData().getChatTurns(chatId as string),
  });
  const turns = query.data;

  // Pages this lookup has asked for, and whether one is in flight. Refs, not
  // state: neither changes what is on screen by itself — the page landing does,
  // through the query's own data.
  const pagesRead = useRef(0);
  const reading = useRef(false);
  useEffect(() => {
    pagesRead.current = 0;
  }, [chatId]);

  const source = chatData();
  const older = enabled && chatId && source.transcriptHistory ? source.transcriptHistory(chatId) : null;
  const satisfied = turns !== undefined && until !== undefined && until(turns);
  const searching =
    enabled &&
    turns !== undefined &&
    !satisfied &&
    (older?.hasOlder ?? false) &&
    pagesRead.current < LOOKUP_PAGE_BUDGET;

  useEffect(() => {
    if (!searching || !chatId || reading.current) return;
    const read = chatData().loadOlderTurns;
    if (!read) return;
    reading.current = true;
    pagesRead.current += 1;
    void read
      .call(chatData(), chatId)
      .catch(() => undefined)
      .finally(() => {
        reading.current = false;
        // The page is folded; re-read so this render — and the surface above it
        // — sees the turns it brought. A failed read re-reads too, and the
        // budget is what stops a source that keeps refusing.
        void queryClient.invalidateQueries({ queryKey: chatKeys.turns(chatId) });
      });
    // `turns` rides the deps so the next page is asked for once the last one has
    // landed: `searching` stays true across the whole walk, and an effect keyed
    // only on it would fire once and stop one page in.
  }, [searching, chatId, turns, queryClient]);

  return { turns: turns ?? [], loading: query.isPending, searching, error: query.isError };
}

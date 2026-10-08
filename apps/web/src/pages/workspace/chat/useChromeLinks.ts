// The chat chrome's quick links and their counts. Knowledge currently downloads a catalogue page
// and lineage runs the roots reading to obtain their totals; separate webviews do not share these
// query caches. Artifacts alone are counted from the transcript already in hand. A count the daemon
// has not answered yet stays undefined, so the link renders without claiming zero.
//
// A link is offered only where the shell says it leads somewhere (`chatRoutes()`): the editor opens
// knowledge as its own tab, and the portal offers neither key — its Knowledge page is reached from
// the nav, and it has no lineage surface at all. A key that reported "Lineage 0" and then did
// nothing at all — which is what the browser rendered — teaches the reader that the chrome is
// decorative.

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import type { ConversationTurn } from "@alkera/chat-model";
import type { ChromeLink } from "@alkera/ui";

import { CONTEXT_LIST_KEY, CONTEXT_LIST_LIMIT } from "./data/contextList";
import { chatRoutes } from "./chatRoutes";
import { chatData } from "./data";
import { collectBlobs } from "./blobModel";

/** `lineage.roots` reports the total beside its page, but the daemon still scans and ranks the
 *  graph to produce that envelope; this limit reduces payload size, not read cost. */
const LINEAGE_ROOTS_KEY = ["ide", "lineage", "roots"] as const;
const LINEAGE_ROOTS_LIMIT = 1;

// A header count is an ornament, not a readout. It revalidates on a daemon
// reconnect (see useDaemonDataRefresh) and otherwise rides a minute of
// staleness rather than polling behind the chat.
const COUNT_STALE_MS = 60_000;

/** `turns` is the transcript the artifacts count is read off. The home surface
 *  has no transcript, so it passes none and the artifacts link renders without
 *  a badge -- unknown, which is the truth there, rather than a zero. */
/** The Artifacts key is off the chat header for now; the results page lives on /objects. */
const ARTIFACTS_KEY_HIDDEN = true;

export function useChromeLinks(turns?: ConversationTurn[]): ChromeLink[] {
  const routes = chatRoutes();
  const knowledge = useQuery({
    queryKey: CONTEXT_LIST_KEY,
    queryFn: () => chatData().listContext({ limit: CONTEXT_LIST_LIMIT }),
    staleTime: COUNT_STALE_MS,
    // No surface behind the key, no reading for it: a count nobody can act on
    // is a request nobody asked for.
    enabled: routes.knowledge !== null,
  });
  const lineage = useQuery({
    queryKey: LINEAGE_ROOTS_KEY,
    queryFn: () => chatData().lineageRoots(LINEAGE_ROOTS_LIMIT),
    staleTime: COUNT_STALE_MS,
    enabled: routes.lineage !== null,
  });


  const artifacts = useMemo(() => (turns ? collectBlobs(turns).length : undefined), [turns]);

  const links: ChromeLink[] = [];
  if (routes.knowledge) links.push({ id: "knowledge", label: "Knowledge", count: knowledge.data?.total });
  if (routes.lineage) links.push({ id: "lineage", label: "Lineage", count: lineage.data?.total_nodes });
  // Artifacts are this chat's own results; every shell has somewhere to put
  // them (`routes.results`). The key is hidden for now (the results page stays
  // reachable from /objects) — flip ARTIFACTS_KEY_HIDDEN to offer it again.
  if (!ARTIFACTS_KEY_HIDDEN) links.push({ id: "artifacts", label: "Artifacts", count: artifacts });
  return links;
}

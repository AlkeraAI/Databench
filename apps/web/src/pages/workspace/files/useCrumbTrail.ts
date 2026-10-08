/**
 * The breadcrumb trail, as its own hook.
 *
 * It lives beside the browser rather than inside the Files page because the
 * page is not the only surface that shows a trail: anything that mounts
 * `FilesBrowser` over a folder — the chat's file explorer among them — walks
 * the same way and must draw the same segments.
 */

import { useEffect, useRef, useState } from "react";

import type { Item } from "@/api/files";
import type { Crumb } from "./Breadcrumbs";
import { displayNameOf, isHome, navIconFor } from "@/lib/files/columns";

/** The trail the session actually walked.
 *
 *  A node read carries its parent but not its ancestors, so the trail is kept as
 *  the path taken: descending into a child appends, returning to a folder already
 *  on the trail truncates back to it, and arriving anywhere else (a rail place, a
 *  search hit, a feed row) starts the trail over at that node. */
export function useCrumbTrail(
  current: Item | undefined,
  dressedAs: Item | undefined = undefined,
): readonly Crumb[] {
  const [trail, setTrail] = useState<readonly Crumb[]>([]);
  // The trail a node's arrival started over, kept until the next node: whether
  // the node is dressed as a chat is known one read after the node itself, and
  // a chat's working directory sits where the chat does — so the trail the
  // undressed draw discarded is the one the dressed draw continues.
  const discarded = useRef<{ by: string; trail: readonly Crumb[] } | null>(null);
  useEffect(() => {
    if (!current) return;
    // A chat's files are browsed at the chat's working directory, and the trail
    // shows that node AS the chat — its title and its mark — because the
    // directory's own name is the box's business and the conversation is what
    // the person opened. The segment still addresses the node the page is on.
    const shown = dressedAs ?? current;
    const mark = navIconFor(shown);
    const crumb: Crumb = {
      id: current.id,
      name: displayNameOf(shown),
      ...(mark === null ? {} : { icon: mark }),
      ...(isHome(shown) ? { home: true } : {}),
    };
    setTrail((before) => {
      // The chat folder itself is never a segment: a deep link to it lands on
      // its working directory, which then stands where the chat would have.
      const previous =
        dressedAs === undefined ? before : before.filter((segment) => segment.id !== dressedAs.id);
      const at = previous.findIndex((segment) => segment.id === crumb.id);
      if (at >= 0) {
        // Re-drawn, not merely kept. A draw that had started the trail over
        // resumes the trail it discarded when the dressed node continues it.
        const stash = discarded.current;
        const resumed = stash?.by === crumb.id ? stash.trail[stash.trail.length - 1] : undefined;
        if (at === 0 && stash !== null && resumed !== undefined && shown.parentId === resumed.id) {
          discarded.current = null;
          return [...stash.trail, crumb];
        }
        return [...previous.slice(0, at), crumb];
      }
      // Only a child of the last crumb extends the trail. A rail place, a search
      // hit or a feed row lands somewhere else in the tree, and a trail that kept
      // growing through those read `Home / Teams / Shared` for three siblings.
      const last = previous[previous.length - 1];
      if (last && shown.parentId === last.id) {
        discarded.current = null;
        return [...previous, crumb];
      }
      discarded.current = { by: crumb.id, trail: previous };
      return [crumb];
    });
  }, [current, dressedAs]);
  return trail;
}

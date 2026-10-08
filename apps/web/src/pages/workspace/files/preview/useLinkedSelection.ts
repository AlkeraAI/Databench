// Keeping the listing and the preview looking at the same row.
//
// Once a file is open in the large preview, the way through a folder is the
// arrow keys — and stepping there has to move the listing too, or closing the
// preview drops the reader back on whatever row they started from. So the row
// the preview is on and the row the listing has selected are ONE piece of state,
// held here, and the page reads both off it.
//
// Which rows a step lands on is the registry's answer, not a list of extensions:
// a step goes to rows something can actually draw, so folders, pointers and a
// file no renderer claims are passed over rather than opening a card that says
// there is nothing to see. That also means a type taught to the registry
// tomorrow becomes steppable with no change here.

import { FALLBACK_RENDERER_ID, planPreview } from "@alkera/ui";
import { useCallback, useMemo, useState } from "react";

import type { Item } from "@/api/files";

import { previewFacts } from "./usePreviewContent";

/** Whether anything in the registry draws this row. A folder is not a file; a
 *  file past its renderer's budget plans to the fallback and is not previewable
 *  either, which is what keeps a step from landing on a card.
 *
 *  A file whose bytes have not arrived is the exception: it plans to the fallback
 *  because nobody can draw nothing, but opening it is exactly right — the card
 *  names the machine it is waiting on. Sending it to a tab instead would hand the
 *  reader an empty download. */
export function isPreviewable(item: Item): boolean {
  if (item.kind !== "file") return false;
  try {
    const plan = planPreview(previewFacts(item));
    return plan.unsynced || plan.renderer.id !== FALLBACK_RENDERER_ID;
  } catch {
    // The registry answers only when its fallback has been unregistered — a host
    // that broke it should not also lose the listing.
    return false;
  }
}

/** What kind of preview this row opens — the registry's own answer for these
 *  bytes, which is what the address carries so a link says what the reader will
 *  be looking at. The sniffed mime decides it, never the extension. */
export function previewKindOf(item: Item): string {
  try {
    return planPreview(previewFacts(item)).renderer.id;
  } catch {
    return FALLBACK_RENDERER_ID;
  }
}

export interface LinkedSelectionOptions {
  /** The rows on screen, in the order they are listed. */
  rows: readonly Item[];
  /** Move the listing's selection — a step in the preview moves the row under it. */
  onSelect?: (id: string) => void;
  /** Put focus back on this row when the preview closes. The focus trap restores
   *  the opener where it still exists; a listing that re-rendered underneath it
   *  needs the id to find the row again. */
  onReturnFocus?: (id: string) => void;
}

export interface LinkedSelection {
  /** The row the preview is on, read fresh from `rows` so a refetch's new etag
   *  reaches the preview. */
  item: Item | undefined;
  open: boolean;
  openOn: (item: Item) => void;
  close: () => void;
  /** Absent at the ends of the run — the preview offers no key it cannot use. */
  onPrev?: () => void;
  onNext?: () => void;
}

export function useLinkedSelection({
  rows,
  onSelect,
  onReturnFocus,
}: LinkedSelectionOptions): LinkedSelection {
  // The row is held as a whole item, not an id: a row that leaves the listing
  // (trashed, moved, filtered out) must keep the preview on screen saying so,
  // rather than blanking it.
  const [held, setHeld] = useState<Item | null>(null);

  const current = useMemo(() => {
    if (!held) return undefined;
    return rows.find((row) => row.id === held.id) ?? held;
  }, [held, rows]);

  const steps = useMemo(() => rows.filter(isPreviewable), [rows]);
  const index = current ? steps.findIndex((row) => row.id === current.id) : -1;

  const goTo = useCallback(
    (row: Item) => {
      setHeld(row);
      onSelect?.(row.id);
    },
    [onSelect],
  );

  const close = useCallback(() => {
    if (held) onReturnFocus?.(held.id);
    setHeld(null);
  }, [held, onReturnFocus]);

  const prev = index > 0 ? steps[index - 1] : undefined;
  const next = index >= 0 && index < steps.length - 1 ? steps[index + 1] : undefined;

  return {
    item: current,
    open: current !== undefined,
    openOn: goTo,
    close,
    onPrev: prev ? () => goTo(prev) : undefined,
    onNext: next ? () => goTo(next) : undefined,
  };
}

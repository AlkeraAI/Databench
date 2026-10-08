// What a drag means while it is in the air, and what a drop may do — decided
// here, with plain data in and plain data out, so the browser's rows, the
// trail and the listing all answer the same way and a test needs no pointer.
//
// Two drags share one wire. A drag from the desktop carries `Files` in the
// transfer's `types` and is an UPLOAD into whatever folder it lands on. A drag
// that started on a row carries {@link MOVE_MIME} and is a MOVE. During
// `dragover` the browser hides the payload (only `types` is readable), so the
// kind of drag is told from the types and the rows being carried are kept by
// the page that started the drag, not read back off the event.

import type { DragEvent } from "react";

import type { Item } from "@/api/files";
import { FILES_UPLOAD_CONCURRENCY } from "@/lib/limits";
import { isPathWithin } from "@/lib/paths";

import { displayNameOf } from "@/lib/files/columns";
import { dropVerdict, leasedFolderRefusal, MOVE_MIME, type DropTarget } from "./dropHandlers";

/** The `DataTransfer` surface a drag-over reads. `types` is the only field the
 *  browser exposes while the drag is still in the air. */
export interface DragTypes {
  readonly types?: readonly string[] | DOMStringList | null;
}

function typeList(transfer: DragTypes | null | undefined): readonly string[] {
  const types = transfer?.types;
  if (!types) return [];
  return Array.from(types as ArrayLike<string>);
}

/** A drag from the desktop: the transfer offers files. */
export function isFilesDrag(transfer: DragTypes | null | undefined): boolean {
  return typeList(transfer).includes("Files");
}

/** A drag that started on one of the browser's rows. */
export function isMoveDrag(transfer: DragTypes | null | undefined): boolean {
  return typeList(transfer).includes(MOVE_MIME);
}

/** Either kind the browser knows how to land. Anything else (a dragged link,
 *  selected text) is left to the browser's default, which is to do nothing. */
export function isKnownDrag(transfer: DragTypes | null | undefined): boolean {
  return isFilesDrag(transfer) || isMoveDrag(transfer);
}

/** The node under a drop, as the verdicts read it. */
export interface MoveTargetNode extends DropTarget {
  /** The slash path, when the listing carries one. It is how "inside its own
   *  subtree" is told without a second read. */
  path?: string | null;
}

/** Whether `target` is `subject` or sits anywhere under it. A folder dropped
 *  into its own subtree would cut it loose from the tree, which the server
 *  refuses with a cycle error — but the refusal belongs here, before a request. */
export function isInsideSubtree(target: { id: string; path?: string | null }, subject: Item): boolean {
  if (target.id === subject.id) return true;
  if (subject.kind !== "folder") return false;
  const root = subject.path;
  const under = target.path;
  if (!root || !under) return false;
  return isPathWithin(root, under);
}

/** Where the dragged rows are being taken FROM: the listing they were picked
 *  up in. Moving a row out needs write on that folder just as putting it down
 *  needs write on the destination. */
export interface MoveSource {
  name: string;
  canWrite: boolean;
  /** A machine holds the listing under a lease: nothing leaves it either. */
  leased?: boolean;
}

export interface MoveDecision {
  /** The rows a request is sent for. Empty when the drop is refused OR when
   *  every row already lives in the target. */
  moves: Item[];
  /** Rows already in the target folder: a drop onto where they are is nothing. */
  unchanged: number;
  /** Why nothing is sent, when nothing is. `null` for an accepted drop. */
  refusal: string | null;
}

/**
 * What a row drop may do. Decided whole: one row that cannot move refuses the
 * drop for all of them, because a drag of five rows that moves three is a
 * surprise nobody asked for.
 */
export function moveVerdict(
  subjects: readonly Item[],
  target: MoveTargetNode | null,
  source: MoveSource,
): MoveDecision {
  const verdict = dropVerdict(target);
  if (!verdict.accepted || !target) {
    return { moves: [], unchanged: 0, refusal: verdict.accepted ? null : verdict.reason };
  }
  if (!source.canWrite) {
    return {
      moves: [],
      unchanged: 0,
      refusal: `You do not have permission to move items out of ${source.name}.`,
    };
  }
  if (source.leased === true) {
    return { moves: [], unchanged: 0, refusal: leasedFolderRefusal(source.name) };
  }
  const moves: Item[] = [];
  let unchanged = 0;
  for (const subject of subjects) {
    if (isInsideSubtree(target, subject)) {
      const name = displayNameOf(subject);
      return {
        moves: [],
        unchanged: 0,
        refusal:
          target.id === subject.id
            ? `${name} cannot be moved into itself.`
            : `${name} cannot be moved into a folder inside it.`,
      };
    }
    if (subject.parentId === target.id) {
      unchanged += 1;
      continue;
    }
    moves.push(subject);
  }
  return { moves, unchanged, refusal: null };
}

/** True when the pointer left the element for somewhere outside it — not for a
 *  child. `dragleave` fires on every child boundary, so a highlight cleared on
 *  the bare event flickers as the pointer crosses a row's cells. */
export function leftElement(event: DragEvent<HTMLElement>): boolean {
  const related = event.relatedTarget;
  if (!(related instanceof Node)) return true;
  return !event.currentTarget.contains(related);
}

/** How many uploads a dropped batch keeps in flight at once. Small on purpose: a
 *  folder of a thousand files must not open a thousand sessions, and three keeps
 *  a link busy without starving the listing's own reads. */
export const UPLOAD_CONCURRENCY = FILES_UPLOAD_CONCURRENCY;

/** How long a lane held back by a lowered limit waits before asking again. */
export const POOL_HOLD_MS = 200;

/**
 * Run `work` over `items` with at most `limit` in flight, in order of start.
 * `shouldStop` is asked before each start, so a batch can be halted after the
 * server says no more will fit; whatever is already in flight is left to
 * settle on its own.
 *
 * One item throwing never strands the items behind it. A lane that simply
 * awaited `work` died with the first rejection it saw, so three lanes over
 * twelve files meant nine files the server was never even asked about — and
 * nothing on screen said so. Every item gets its turn; the rejections are
 * collected and thrown together once the pool has drained, so a caller that
 * handles nothing still learns something went wrong.
 */
export async function runPool<T>(
  items: readonly T[],
  limit: number | (() => number),
  work: (item: T, index: number) => Promise<void>,
  shouldStop: () => boolean = () => false,
): Promise<void> {
  let next = 0;
  const failures: unknown[] = [];
  // A live limit is read before every start, so a lane past the server's
  // current guidance holds its next item back until the guidance rises or the
  // lanes under it drain the batch; the first lane always runs.
  const allowed = typeof limit === "function" ? limit : () => limit;
  const lanes = Array.from({ length: Math.max(1, Math.min(allowed(), items.length)) }, async (_, lane) => {
    for (;;) {
      if (shouldStop()) return;
      if (lane > 0 && lane >= allowed()) {
        if (next >= items.length) return;
        await new Promise((resolve) => setTimeout(resolve, POOL_HOLD_MS));
        continue;
      }
      const index = next;
      next += 1;
      if (index >= items.length) return;
      try {
        await work(items[index] as T, index);
      } catch (error) {
        failures.push(error);
      }
    }
  });
  await Promise.all(lanes);
  if (failures.length > 0) {
    throw new AggregateError(failures, `${failures.length} of ${items.length} failed`);
  }
}

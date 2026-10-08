/**
 * The Owner column's one cell.
 *
 * The listing carries the owner as a principal id (`attrs.owner`) and, beside
 * it, the label the server resolved for a person to read (`ownerName`): the
 * owner's name, or "Agent" for a row a machine is recorded as owning. An id is
 * the right thing on the wire and the wrong thing on screen, so the cell never
 * prints one. It renders, in order of preference: "You" for the signed-in
 * person's own rows, then the server's label, then a dash.
 */

import type { Item } from "@/api/files";
import { useCurrentUser } from "@/api/auth";

/** Nothing is known about the owner. */
const UNKNOWN = "—";

export interface OwnerLabel {
  /** What the cell prints. */
  readonly text: string;
}

/** The owner label the server sent, when it sent one. */
function ownerName(item: Item): string | null {
  const named = item.ownerName;
  return typeof named === "string" && named !== "" ? named : null;
}

/** The owner principal id the listing carries. */
export function ownerId(item: Item): string | null {
  const owner = item.attrs?.owner;
  return typeof owner === "string" && owner !== "" ? owner : null;
}

/**
 * What the Owner cell shows for one row, given who is asking.
 *
 * Pure, so every branch is reachable from a test without a signed-in session.
 */
export function ownerLabel(item: Item, meId: string | null | undefined): OwnerLabel {
  const id = ownerId(item);
  if (id !== null && meId != null && meId !== "" && id === meId) return { text: "You" };
  return { text: ownerName(item) ?? UNKNOWN };
}

export interface OwnerCellProps {
  readonly item: Item;
}

/** The rendered cell. The current user is one cached query for the whole page,
 *  not one per row, because every row reads the same key. */
export function OwnerCell({ item }: OwnerCellProps) {
  const me = useCurrentUser();
  return <span>{ownerLabel(item, me.data?.id).text}</span>;
}

export default OwnerCell;

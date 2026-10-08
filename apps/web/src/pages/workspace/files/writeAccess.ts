/**
 * One answer to "will this folder take what I put in it".
 *
 * Fail closed: a row that does not say is not a row that said yes. A listing
 * answers with capabilities, and anything that arrives without them — an older
 * server, a trimmed projection, a row the client built itself — is treated as a
 * folder the reader may not write to, because the alternative is offering a
 * write the server then refuses.
 *
 * It lives on its own because two surfaces decide it about the same folders: the
 * drop target under a drag, and the destination picker. While they each spelled
 * it, they disagreed — a folder a drag refused was offered to a move.
 */

import { granted } from "@/lib/capabilities";

/** The shape both callers have: a row, or a drop target built from one. */
export interface WriteTarget {
  capabilities?: { can_write?: boolean } | null;
}

/** Whether the reader may add something to this folder. */
export function acceptsWrites(target: WriteTarget | null | undefined): boolean {
  return granted(target?.capabilities?.can_write);
}

// What a `/files/<id>` link actually lands on.
//
// The id in the URL is whatever was shared — a folder, a file inside a folder
// the reader can also open, or a file shared ALONE, with everything above it
// closed to them. Those are not three renderings of one screen: the third has
// no siblings to list, no path to draw and no folder above to walk to, and a
// page that assumed otherwise would leak the names of a folder the reader was
// never granted.
//
// The decision is made here, from the two reads the page already makes (the
// node, and the folder it names as its parent), so the page's branch is a
// lookup rather than a chain of conditions repeated in the trail, the toolbar
// and the listing.

import { ApiError } from "@/api/errors";

/** The landing the page renders.
 *
 *  `pending` — a read is still in flight; `folder` — list it; `file-in-folder`
 *  — list the parent with the file selected and its preview open; `file-solo` —
 *  the file alone, with nothing of its folder shown; `not-here` — the id names
 *  nothing the reader may have; `error` — the read failed for some other
 *  reason, which is a fault to retry rather than an answer. */
export type LandingState =
  | "pending"
  | "folder"
  | "file-in-folder"
  | "file-solo"
  | "not-here"
  | "error";

/** The shape the decision needs off a node — deliberately narrower than `Item`
 *  so the rule can be exercised with the two fields it actually reads. */
export interface LandingNode {
  readonly kind?: string;
  readonly parentId?: string | null;
}

export interface LandingReads {
  /** The node the URL names. */
  node: LandingNode | undefined;
  nodeError?: unknown;
  /** The node's parent, read only when the node turns out to be a file. */
  parent?: LandingNode | undefined;
  parentError?: unknown;
}

/** A refusal that is an ANSWER: the node is gone, it is not the reader's to
 *  see, or the id cannot name a node at all. The first two are told the same way
 *  on purpose — a page that distinguished them would confirm the existence of
 *  something the reader may not know about.
 *
 *  The third joins them because it is an answer too: an id the drive's validator
 *  rejects (400, 422) names nothing and never will, and reading only 403/404 left
 *  `/files/not-a-uuid` on "Opening…" for good. The route guard turns most of those
 *  away before the read; this is the floor under it, for a link built by an older
 *  client or an id shape the server tightens later. */
const REFUSED: ReadonlySet<number> = new Set([400, 403, 404, 422]);

function refused(error: unknown): boolean {
  return error instanceof ApiError && REFUSED.has(error.status);
}

function failed(error: unknown): boolean {
  return error !== undefined && error !== null;
}

export function landingState({ node, nodeError, parent, parentError }: LandingReads): LandingState {
  if (failed(nodeError)) return refused(nodeError) ? "not-here" : "error";
  if (node === undefined) return "pending";
  // Only a file has bytes to preview. Everything else — a folder, a chat, a row
  // a later release teaches the drive — is listed, which is also what keeps an
  // unknown kind from landing on a dead end.
  if (node.kind !== "file") return "folder";
  const up = node.parentId;
  if (up === null || up === undefined || up === "") return "file-solo";
  // The folder is closed to this reader: the file stands alone. Any OTHER
  // refusal is a fault rather than an answer — the listing says so itself and
  // can be retried, which "its folder isn't shared with you" would deny.
  if (refused(parentError)) return "file-solo";
  if (failed(parentError)) return "file-in-folder";
  if (parent === undefined) return "pending";
  return "file-in-folder";
}

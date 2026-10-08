// What `/objects/:objectId` shows when the object cannot be shown.
//
// Two different answers. The server saying the object is not there — deleted,
// never existed, or not shared with this reader, drawn alike so a guessed id
// learns nothing — is final: one line and the way back to Files, where objects
// are opened from. Anything else is a read that failed and may work again, so it
// offers the retry.

import type { ReactElement } from "react";
import { Link } from "react-router-dom";

import { EmptyState } from "@alkera/ui";

import { ApiError } from "../../../api/errors";
import { NOT_HERE } from "../files/NotHere";

/** The statuses that answer "this reader has no such object". A 403 reads the
 *  same as a 404 so the page never confirms that an unshared id exists. */
const ANSWERED: ReadonlySet<number> = new Set([403, 404, 410]);

export function objectIsGone(error: unknown): boolean {
  return error instanceof ApiError && ANSWERED.has(error.status);
}

export const OBJECT_UNAVAILABLE = {
  gone: NOT_HERE,
  goneAction: "Go to Files",
  failed: "This object could not be loaded.",
  retry: "Try again",
} as const;

export function ObjectUnavailable({ error, onRetry }: { error: unknown; onRetry: () => void }): ReactElement {
  if (objectIsGone(error)) {
    return (
      <EmptyState
        title={OBJECT_UNAVAILABLE.gone}
        action={
          <Link className="alk-btn" to="/files">
            {OBJECT_UNAVAILABLE.goneAction}
          </Link>
        }
      />
    );
  }
  return (
    <EmptyState
      tone="alert"
      title={OBJECT_UNAVAILABLE.failed}
      action={
        <button type="button" className="alk-btn" onClick={onRetry}>
          {OBJECT_UNAVAILABLE.retry}
        </button>
      }
    />
  );
}

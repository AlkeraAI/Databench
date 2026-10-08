import { PageError, Skeleton } from "@alkera/ui";

import { failureSentence } from "../../api/errors";
import { retryAfterSeconds } from "../../api/retry";

/**
 * The placeholder a route guard shows while it resolves who the user is — never the
 * empty or error surface, which would flash before the decision is known. Shared by
 * every guard (RequireAuth and the permission gates) so they stay identical.
 */
export function GateLoading({ label }: { label: string }) {
  return (
    <div className="alk-authgate" role="status" aria-label={label}>
      <Skeleton width={220} height={14} />
    </div>
  );
}

/**
 * What a guard shows when the read behind it FAILED rather than answered.
 *
 * A guard sits outside the app shell, so whatever it renders is the whole page. A role nobody
 * could establish is not a role that was refused, so the reader gets the same plate every other
 * failed page gets, with the action that asks again, rather than a bounce to the dashboard.
 */
export function GateError({ error, onRetry }: { error?: unknown; onRetry: () => void }) {
  return (
    <div className="alk-authgate">
      <PageError
        of="this page"
        message={failureSentence(error)}
        retryAfterSeconds={retryAfterSeconds(error)}
        onRetry={onRetry}
      />
    </div>
  );
}

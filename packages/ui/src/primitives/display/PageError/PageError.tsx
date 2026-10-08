import { Button } from "../../controls/Button";
import { EmptyState } from "../EmptyState";

// The one plate a page shows when the data behind it did not arrive, so every surface treats a
// failed read the same way: a headline that names what failed, at most one sentence, one action
// that re-runs the read, and the raw failure tucked under a disclosure where it cannot headline.
//
// Pure presentation. The caller owns the retry (a refetch) and the noun; nothing here fetches.

/** The sentence shown when the caller has none of its own — the common case, since a transport
 *  failure has no server message to quote. */
const DEFAULT_MESSAGE = "The request did not reach the server.";

export interface PageErrorProps {
  /** What failed to load, spelled as the reader would name it and read straight into the headline:
   *  "your workspace", "teams", "sign-in methods". Lower case — the headline supplies the sentence. */
  of: string;
  /** One sentence naming the cause. Defaults to the transport sentence. */
  message?: string;
  /** Re-run the read. Omitted when the caller has nothing to re-run (the action is then hidden
   *  rather than shown dead). */
  onRetry?: () => void;
  /** Seconds the server asked the caller to wait, from a `Retry-After`. Adds the one line that
   *  tells the reader whether pressing Retry now is worth anything. */
  retryAfterSeconds?: number;
  /** The raw technical failure, under a disclosure. */
  details?: string;
  /** `lg` (default) replaces a whole view; `md` sits inside a card or a panel body. */
  size?: "sm" | "md" | "lg";
  className?: string;
}

export function PageError({
  of,
  message,
  onRetry,
  retryAfterSeconds,
  details,
  size = "lg",
  className,
}: PageErrorProps) {
  const wait =
    retryAfterSeconds != null && retryAfterSeconds > 0
      ? ` Try again in ${retryAfterSeconds} ${retryAfterSeconds === 1 ? "second" : "seconds"}.`
      : "";
  return (
    <EmptyState
      className={className}
      tone="alert"
      role="alert"
      size={size}
      title={`Could not load ${of}`}
      body={`${message ?? DEFAULT_MESSAGE}${wait}`}
      details={details}
      action={
        onRetry ? (
          <Button variant="secondary" onClick={onRetry}>
            Retry
          </Button>
        ) : undefined
      }
    />
  );
}

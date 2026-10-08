import { Button, EmptyState } from "@alkera/ui";

// The dashboard's first-class ERROR surface, built on the shared @alkera/ui `EmptyState` plate so
// every page's fail-to-load surface reads the same. Only the bench mark + the copy are
// dashboard-specific; the centred plate, the Newsreader heading, the raw-detail disclosure, and the
// action layout come from the shared base. (There is no empty surface: every card on the overview
// reads correctly at zero, so a new seat gets the working page instead of a dead end.)
//
// The error surface follows the convention for errors (Primer/Material): a plain alert mark,
// plain-language copy, and the raw technical detail tucked under a "Details" disclosure rather than
// shouted as the headline (handled by EmptyState's `details` slot).

/** A warning triangle with an exclamation — the conventional alert mark. The triangle spans y4–y20
 *  so the glyph is vertically centred in the 24-unit box. Inherits the plate's alert color. */
function AlertTriangleMark() {
  return (
    <svg
      width={48}
      height={48}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.4}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {/* rounded-corner triangle outline */}
      <path d="M10.3 4.8a2 2 0 013.4 0l7.5 13a2 2 0 01-1.7 3H4.5a2 2 0 01-1.7-3z" />
      {/* the exclamation: a vertical stroke and a dot near the base */}
      <path d="M12 9.5v4.5" />
      <path d="M12 17.2h.01" />
    </svg>
  );
}

/** Shown when a fetch failed. The headline is plain language; the raw technical error (when there is
 *  one) hides under a "Details" disclosure so support can recover it without it being the headline. */
export function DashboardError({ message, onRetry }: { message: string | null; onRetry: () => void }) {
  return (
    <EmptyState
      tone="alert"
      icon={<AlertTriangleMark />}
      title={"We couldn’t load your overview"}
      body="Try again. If it keeps happening, the detail below can help support."
      details={message ?? undefined}
      action={
        <Button variant="secondary" onClick={onRetry}>
          Try again
        </Button>
      }
    />
  );
}

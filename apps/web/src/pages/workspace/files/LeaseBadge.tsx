// The lease badge: who has a folder out for local use, on which machine, since
// when, and how far behind their last sync is.
//
// Rendered on the leased folder itself in the list, the grid and the breadcrumb,
// and in full in the right pane. Items *inside* a leased folder render the
// quieter read-only line instead. Nothing here polls: the relative time ticks on
// a local clock and the facet itself is replaced by the delta feed.

import type { Item } from "@/api/files";
import { holderInitial, useLeaseView, type LeaseView } from "./useLeaseFacet";

export type LeaseBadgeVariant = "list" | "grid" | "breadcrumb" | "pane";

export interface LeaseBadgeProps {
  item: Item | undefined | null;
  /** Where it is being rendered; only the density changes, never the facts. */
  variant?: LeaseBadgeVariant;
  /** Pinned in tests so a relative time is deterministic. */
  tickMs?: number;
}

/** The initial of whoever the sentence names, never an image request from a
 *  row. It is taken from the sentence's own subject rather than from the raw
 *  holder, which for a box is an allocation uuid — and rendered as a lone
 *  digit in front of the badge. */
function HolderAvatar({ holder }: { holder: string }) {
  return (
    <span className="alk-files-lease__avatar" aria-hidden="true">
      {holderInitial(holder)}
    </span>
  );
}

/** The server says the holder is alive but has fallen behind its own sync
 *  cadence, so what is shown here is older than the holder's disk. */
function StaleMarker() {
  return (
    <span className="alk-files-lease__stale" title="The holder's sync has fallen behind">
      behind
    </span>
  );
}

/** The badge says who has the folder and stops: the start time and the sync
 *  age live in the pane and the leases list, where a reader goes for them. */
function sentence(view: LeaseView): string {
  return view.summary;
}

/** The badge on the leased folder. Renders nothing when the item is not leased,
 *  so a caller can drop it into every row unconditionally. */
export function LeaseBadge({ item, variant = "list", tickMs }: LeaseBadgeProps) {
  const view = useLeaseView(item, tickMs);
  if (!view) return null;
  return (
    <span
      className={`alk-files-lease alk-files-lease--${variant}`}
      data-mine={view.mine ? "true" : "false"}
      data-stale={view.stale ? "true" : "false"}
    >
      <HolderAvatar holder={view.mine ? "you" : (view.chat ?? view.holder)} />
      <span className="alk-files-lease__text">{sentence(view)}</span>
      {view.stale ? <StaleMarker /> : null}
    </span>
  );
}

/** The quieter state on an item *inside* a leased folder: it can be read, it
 *  cannot be written, and the last sync says how current it is. */
export function LeaseReadOnlyState({ item, tickMs }: LeaseBadgeProps) {
  const view = useLeaseView(item, tickMs);
  if (!view) return null;
  const synced = view.synced ? `, synced ${view.synced}` : "";
  return (
    <span
      className="alk-files-lease alk-files-lease--inside"
      data-stale={view.stale ? "true" : "false"}
    >
      <span className="alk-files-lease__text">{`Read-only${synced}`}</span>
      {view.stale ? <StaleMarker /> : null}
    </span>
  );
}

/** The right pane's full facet: every field the server sent, spelled out. */
export function LeaseFacetPane({ item, tickMs }: LeaseBadgeProps) {
  const view = useLeaseView(item, tickMs);
  if (!view) return null;
  return (
    <dl className="alk-files-lease__facet" aria-label="Lease">
      <dt>Holder</dt>
      <dd>{view.mine ? `${view.holder} (you)` : view.holderLabel}</dd>
      <dt>Machine</dt>
      <dd>{view.machine}</dd>
      <dt>Purpose</dt>
      <dd>{view.facet.purpose}</dd>
      <dt>Since</dt>
      <dd>{view.since ?? "unknown"}</dd>
      <dt>Last synced</dt>
      <dd>
        {view.synced ?? "not yet"}
        {view.stale ? <StaleMarker /> : null}
      </dd>
      <dt>Expires</dt>
      <dd>{view.expires ?? "unknown"}</dd>
    </dl>
  );
}

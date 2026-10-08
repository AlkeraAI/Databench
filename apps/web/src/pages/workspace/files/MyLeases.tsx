// "My leases": the folders this person currently has out for local use, as
// `alkera files mounts` lists them.
//
// One read, `useLeases`, which the server answers with `mine=true`; the same key
// the badges read, so a release refreshes both at once. The relative time ticks
// locally — the rows themselves arrive on the delta feed.

import type { LeaseRow } from "@/api/files";
import { useLeases } from "@/api/files";
import { SOMEWHERE, leaseName, relativeTime, useNow } from "./useLeaseFacet";

export interface MyLeasesProps {
  driveId: string | undefined;
  /** Open one of my leases in the browser. */
  onOpen?: (nodeId: string) => void;
  /** True while the event stream is down, so the read falls back to polling. */
  streamDown?: boolean;
  tickMs?: number;
}

/** One row: the machine it is mounted on and how current it is. A row carries
 *  only what the holder registered itself under, which for a box is its
 *  allocation uuid, so it is named the way every other lease surface names an
 *  unresolved machine rather than printed as an id. */
function LeaseRowLine({ row, now }: { row: LeaseRow; now: number }) {
  const synced = relativeTime(row.lastSyncAt, now);
  return (
    <>
      <span className="alk-files-myleases__machine">
        {leaseName(undefined, row.machine, SOMEWHERE)}
      </span>
      <span className="alk-files-myleases__meta">
        {synced ? `synced ${synced}` : "not synced yet"}
      </span>
    </>
  );
}

export function MyLeases({ driveId, onOpen, streamDown, tickMs }: MyLeasesProps) {
  const now = useNow(tickMs);
  const leases = useLeases(driveId, { streamDown: streamDown ?? false });
  const rows = leases.data ?? [];

  if (leases.isPending) {
    return (
      <p className="alk-files-myleases__empty" aria-busy="true">
        Loading your leases…
      </p>
    );
  }
  if (rows.length === 0) {
    return (
      <p className="alk-files-myleases__empty">
        You have no folders out for local use. Lease one to work on it from your machine.
      </p>
    );
  }

  return (
    <ul className="alk-files-myleases" aria-label="My leases">
      {rows.map((row) => (
        <li key={`${row.nodeId}:${row.epoch}`} className="alk-files-myleases__row">
          {onOpen ? (
            <button type="button" onClick={() => onOpen(row.nodeId)}>
              <LeaseRowLine row={row} now={now} />
            </button>
          ) : (
            <LeaseRowLine row={row} now={now} />
          )}
        </li>
      ))}
    </ul>
  );
}

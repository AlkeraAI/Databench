// What ONE row is doing on the machine, right now.
//
// Renders nothing for a row with no live facet — which is nearly every row —
// so a listing can drop it into every line unconditionally. A state this build
// has no word for renders nothing too: a newer server's spelling must never
// reach a reader raw.

import { formatSize } from "@/lib/files/columns";
import { liveContentChip, liveRowChip } from "../liveRoot/liveCopy";
import type { Item } from "@/api/files";
import { SOMEWHERE, leaseName } from "../useLeaseFacet";

import "./live.css";

type LiveFacet = NonNullable<Item["live"]>;
type LeaseFacet = NonNullable<Item["lease"]>;

export interface LiveRowChipProps {
  live: LiveFacet | null | undefined;
  /** The row's lease facet, for the machine a content chip names. */
  lease?: LeaseFacet | null;
}

/** The states a person caused: a write into the folder on its way to the
 *  machine. They say which way the difference is moving, which the content
 *  state (measured from the machine's side) cannot. */
const INBOUND = new Set(["inbound", "inbound_delete", "inbound_rename"]);

interface Chip {
  label: string;
  state: string;
}

/** Which word a row gets. A write on its way to the machine says so first;
 *  then bytes the machine will not send, and a row whose bytes are on the drive,
 *  on their way, or that the machine has no copy of says nothing. Only a server
 *  that sends no content word at all falls back to what the live plane says the
 *  row is doing. */
function chipFor(live: LiveFacet, lease: LeaseFacet | null | undefined): Chip | null {
  const state = live.state;
  if (state && INBOUND.has(state)) {
    const label = liveRowChip(state);
    return label === null ? null : { label, state };
  }
  // Typed as possibly absent on purpose: a server that predates the content
  // word sends none, and that row falls back to what the live plane says.
  const content: LiveFacet["content"] | undefined = live.content;
  const machine = leaseName(lease?.machine_name, lease?.machine, SOMEWHERE);
  const said = liveContentChip(content, machine);
  if (said !== null && content !== undefined) return { label: said, state: content };
  if (content !== undefined) return null;
  if (!state) return null;
  // The size only means something for a file left on the machine, so it is
  // only spelled there — "uploading… (2 MB)" would be a number the reader
  // cannot act on.
  const onBox = state === "on_box" || state === "deferred";
  const size = onBox && typeof live.box_size === "number" ? formatSize(live.box_size) : null;
  const label = liveRowChip(state, size);
  return label === null ? null : { label, state };
}

export function LiveRowChip({ live, lease }: LiveRowChipProps) {
  if (!live) return null;
  const chip = chipFor(live, lease);
  if (chip === null) return null;
  return <ChipMark chip={chip} />;
}

function ChipMark({ chip }: { chip: Chip }) {
  return (
    <span className="alk-files-live-chip" data-state={chip.state}>
      {chip.label}
    </span>
  );
}

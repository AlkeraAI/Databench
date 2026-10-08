/**
 * "Copy to…" — the folder picker, opened on the reader's own drive.
 *
 * The same picker as Move to…, worded for a copy, and opened where Move to…
 * opens: the folder the rows are in. Opening at the drive root sent the reader
 * away from where they were and made them wait on the root listing before they
 * could pick the folder next door. A place with no folder of its own (Recent,
 * Shared with me) still opens at the reader's home.
 *
 * The rows it acts on are the caller's; this component only picks the place
 * and hands back the node id the duplicate route accepts. A chat picked as the
 * destination is a folder like any other here — the server decides where inside
 * it a copy lands, exactly as it does for a move.
 */

import { useDrive, type Item } from "@/api/files";

import type { Crumb } from "./Breadcrumbs";
import { MoveToDialog } from "./MoveToDialog";
import type { Platform } from "./state/shortcuts";

export interface CopyToDialogProps {
  open: boolean;
  driveId: string | undefined;
  /** The folder the rows are listed in, where the picker opens. Left out, it
   *  opens at the reader's home. */
  startFolderId?: string | undefined;
  startLabel?: string;
  startTrail?: readonly Crumb[];
  /** How many items are being copied, for the title. */
  count: number;
  platform?: Platform;
  /** Test seam for the treegrid's virtualizer (jsdom measures nothing). */
  initialRect?: { width: number; height: number };
  onCancel: () => void;
  /** The destination, and the row it came from when the picker had one. */
  onConfirm: (parentId: string, destination?: Item) => void;
}

export const COPY_HERE = "Copy here";

export function copyToTitle(count: number): string {
  return `Copy ${count} item${count === 1 ? "" : "s"} to…`;
}

export function CopyToDialog({
  open,
  driveId,
  startFolderId,
  startLabel,
  startTrail,
  count,
  platform,
  initialRect,
  onCancel,
  onConfirm,
}: CopyToDialogProps) {
  // Where there is no folder being listed the reader's home stands in; the
  // drive read that names it is the one every Files page already holds.
  const drive = useDrive({ enabled: open && startFolderId === undefined });
  const homeId = drive.data?.homeId ?? undefined;
  if (!open) return null;
  const here = startFolderId !== undefined;
  return (
    <MoveToDialog
      open
      driveId={driveId}
      startFolderId={here ? startFolderId : homeId}
      {...(here ? (startLabel ? { startLabel } : {}) : { startLabel: "My drive" })}
      {...(here && startTrail ? { startTrail } : {})}
      count={count}
      title={copyToTitle(count)}
      confirmLabel={COPY_HERE}
      {...(platform ? { platform } : {})}
      {...(initialRect ? { initialRect } : {})}
      onCancel={onCancel}
      onConfirm={onConfirm}
    />
  );
}

export default CopyToDialog;

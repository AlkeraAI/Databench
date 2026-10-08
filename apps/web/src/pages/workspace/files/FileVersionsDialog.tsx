/**
 * "Versions" — every version a file has had, and the way back to one.
 *
 * The upload prompt promises that replacing a file keeps what was there. This
 * is where that promise is kept: without a surface the old bytes are stored,
 * paid for, and unreachable.
 *
 * A restore is a forward write. The server appends the chosen version's bytes
 * as a NEW head instead of deleting the ones above it, so the list only grows
 * and nothing on screen can be lost by clicking Restore — which is why the row
 * needs no confirmation.
 *
 * Self-sufficient like the share dialog: it takes a drive and a node id and
 * reads the rest itself, so a surface with no Files row in hand opens it with
 * the same props. The node read is not decoration — a restore is fenced on
 * `If-Match` and bumps the version, so the etag a SECOND restore sends has to
 * come from a read that saw the first one land.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button, Callout, Modal, Pill } from "@alkera/ui";

import { useFrames } from "@/api/events/frameBus";
import { isMachineRate, machineRefresh } from "@/api/events/machineRefresh";
import { useItem, useRestoreVersion, useVersions, type VersionList } from "@/api/files";
import { keys } from "@/api/keys";

import { formatSize } from "@/lib/files/columns";
import { filesErrorCopy, type FilesErrorCopy } from "@/lib/files/errors";
import { CAP_REFUSAL, capabilityRefusal } from "./refusalCopy";
// The dialog owns its sheet: it opens from a chat's file tab and files tab,
// chunks that never load the files browser's stylesheet.
import "./file-versions-dialog.css";

/** One row of the history, as the wire spells it. */
type Version = NonNullable<VersionList["versions"]>[number];

/** The history in the order a reader wants it — the version they are at first.
 *
 *  Ordered on `seq`, the server's own ordinal for a version, rather than by
 *  reversing the array: the answer is documented as oldest first, and an
 *  ordering that only holds while that stays true is one nobody would think to
 *  re-check. */
function newestFirst(versions: readonly Version[]): Version[] {
  return [...versions].sort((a, b) => b.seq - a.seq);
}

/** The version the node points at.
 *
 *  `isHead` is the server's own answer and is trusted when it is there. It is
 *  an optional field, so an answer carrying none falls back to the highest
 *  `seq`: a restore appends its bytes on top, so the newest version IS the head
 *  — and a list offering to restore the state the file is already in tells the
 *  reader nothing about where they are. */
function headVersionId(versions: readonly Version[]): string | null {
  const flagged = versions.find((one) => one.isHead === true);
  if (flagged !== undefined) return flagged.id;
  return newestFirst(versions)[0]?.id ?? null;
}

/** When a version was made, to the minute: a file edited live gains one every
 *  few seconds, and a day alone tells them apart for nobody. */
export function versionTime(at: string): string {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Who wrote a version, as the row says it: the person, the agent's
 *  machine, or both; nothing when the drive names neither. */
export function whoWrote(version: Pick<Version, "author" | "machine">): string {
  const parts = [version.author, version.machine ? "Agent" : null];
  return parts.filter((part): part is string => Boolean(part)).join(", ");
}

export interface FileVersionsDialogProps {
  driveId: string | undefined;
  /** The file whose history this is. */
  nodeId: string | undefined;
  open: boolean;
  onClose: () => void;
  /** What to call the file, when the node's own name is not what the reader
   *  knows it by — a chat is stored as `<uuid>.alkerachat`. */
  subjectName?: string;
}

export function FileVersionsDialog({
  driveId,
  nodeId,
  open,
  onClose,
  subjectName,
}: FileVersionsDialogProps) {
  const node = useItem(driveId, open ? nodeId : undefined);
  const versions = useVersions(driveId, open ? nodeId : undefined);
  const restore = useRestoreVersion();

  // The history grows while it is open: a save by the machine, a live
  // document's write-back, somebody's restore. A frame naming the file re-reads
  // it, at a machine's rate when that is what wrote it.
  const queryClient = useQueryClient();
  useFrames(
    (frame) => open && nodeId !== undefined && frame.type === "file_node.changed" && frame.entity_id === nodeId,
    (frame) => {
      if (isMachineRate(frame)) machineRefresh(queryClient).request(keys.files.versions(nodeId));
      else queryClient.invalidateQueries({ queryKey: keys.files.versions(nodeId) }).catch(() => undefined);
    },
  );

  const [refusal, setRefusal] = useState<FilesErrorCopy | null>(null);
  /** The version a restore is running for. Every Restore fences on the same
   *  etag, so a second one sent before the first lands names a version the node
   *  has already left — the whole list is held rather than just the row. */
  const [restoring, setRestoring] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setRefusal(null);
      setRestoring(null);
    }
  }, [open]);

  const listed = useMemo(() => versions.data?.versions ?? [], [versions.data]);
  const rows = useMemo(() => newestFirst(listed), [listed]);
  const head = useMemo(() => headVersionId(listed), [listed]);

  const item = node.data;
  const canWrite = item?.capabilities?.can_write === true;
  // Why not, in the one wording every surface uses for the server's reason.
  const writeRefusal =
    item === undefined
      ? undefined
      : (capabilityRefusal(item, "can_write") ?? CAP_REFUSAL.can_write);

  const restoreOne = useCallback(
    async (versionId: string): Promise<void> => {
      if (!driveId || !nodeId || !item) return;
      setRefusal(null);
      setRestoring(versionId);
      try {
        await restore.mutateAsync({ driveId, itemId: nodeId, etag: item.etag, versionId });
      } catch (error) {
        setRefusal(filesErrorCopy(error));
      } finally {
        setRestoring(null);
      }
    },
    [driveId, nodeId, item, restore],
  );

  // The caller's name for the subject outranks the node's own: it is the one a
  // reader recognises, and it is known before the node read lands.
  const subject = subjectName ?? (item ? item.nameDisplay || item.name : "");
  const title = subject ? `Versions of “${subject}”` : "Versions";

  return (
    <Modal open={open} onClose={onClose} title={title} size="md">
      <div className="alk-files-versions__body">
        {refusal ? (
          <div className="alk-files-versions__error" data-code={refusal.code}>
            <Callout tone="danger" role="alert" title={refusal.title}>
              {refusal.detail || undefined}
            </Callout>
          </div>
        ) : null}

        {versions.isPending ? (
          <p className="alk-files-versions__note">Loading…</p>
        ) : versions.isError ? (
          <p className="alk-files-versions__note" role="status">
            This file&rsquo;s versions could not be read.
          </p>
        ) : rows.length === 0 ? (
          <p className="alk-files-versions__note">This file has no versions.</p>
        ) : (
          <>
            <ul className="alk-files-versions__rows" aria-label="Versions">
              {rows.map((version) => (
                <li key={version.id} className="alk-files-versions__row">
                  <span className="alk-files-versions__who">
                    <span className="alk-files-versions__name">Version {version.seq}</span>
                    <span className="alk-files-versions__meta">
                      {[formatSize(version.size), versionTime(version.createdAt), whoWrote(version)]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  </span>
                  {version.id === head ? (
                    <Pill tone="neutral" size="sm">
                      Current
                    </Pill>
                  ) : (
                    <Button
                      size="sm"
                      variant="secondary"
                      fill="outline"
                      // Every button on the list reads "Restore", so the name a
                      // screen reader announces has to say which one this is.
                      aria-label={`Restore version ${version.seq}`}
                      disabled={!canWrite || restoring !== null}
                      title={canWrite ? undefined : writeRefusal}
                      onClick={() => void restoreOne(version.id)}
                    >
                      Restore
                    </Button>
                  )}
                </li>
              ))}
            </ul>
            {rows.length === 1 ? (
              <p className="alk-files-versions__note">This file has only one version.</p>
            ) : null}
          </>
        )}
      </div>
    </Modal>
  );
}

export default FileVersionsDialog;

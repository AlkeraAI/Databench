/**
 * `/files/trash` — the trashed roots, with what it takes to get one back.
 *
 * Trash lists *deletions*, not nodes: one row is one trash operation, which is why a
 * folder deleted with 40,000 files inside it is a single row and restoring it brings
 * the whole subtree back in one call. The row therefore carries the deletion's facts —
 * where it used to live and how long is left before it is purged — rather than the
 * node's listing columns.
 *
 * Restore lands at the original place; a same-named sibling created since does not make
 * it fail, the server applies the one conflict-rename function and answers with the
 * name it actually used, which this page reports so nobody hunts for the old one.
 */

import { useCallback, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button, ConfirmDialog, ToastViewport, useToasts } from "@alkera/ui";

import {
  useDrive,
  useEmptyTrash,
  useItem,
  useRestoreTrash,
  useTrash,
  useTrashItem,
  type Item,
} from "@/api/files";
import { keys } from "@/api/keys";

import { useLimits } from "@/lib/limits";

import { displayPlaceName } from "./Breadcrumbs";
import { displayNameOf } from "@/lib/files/columns";
import { filesErrorCopy, type FilesErrorContext } from "@/lib/files/errors";
import { ThresholdFooter } from "./ThresholdFooter";
import { useCeilingPager } from "./useSoftThreshold";
import { leaseRefusalContext } from "./useLeaseFacet";

/** One trash row, flattened out of the marker pages. */
export interface TrashRow {
  trashOpId: string;
  item: Item;
  originalParentId: string | null;
  /** The folder it was deleted from, as the server renders it; null once that folder is gone. */
  originalPath: string | null;
  deletedAt: string;
  timeLeftSeconds: number;
}

/** Where a trashed row was deleted from, as the Files trail reads it:
 *  `/home/Dana Okafor/Chats` is "Home › Dana Okafor › Chats". The root
 *  containers take the product's spelling; every folder a person named keeps
 *  its own. */
export function trashLocation(path: string): string {
  const segments = path.split("/").filter((segment) => segment !== "");
  if (segments.length === 0) return displayPlaceName("home");
  return segments.map((segment, index) => (index === 0 ? displayPlaceName(segment) : segment)).join(" › ");
}

/** "29 days left", "4 hours left", "less than a minute left" — the unit a person
 *  actually needs at that distance, never a second-by-second countdown. */
export function timeLeftLabel(seconds: number): string {
  if (seconds <= 0) return "purging now";
  const days = Math.floor(seconds / 86_400);
  if (days >= 1) return `${days} ${days === 1 ? "day" : "days"} left`;
  const hours = Math.floor(seconds / 3_600);
  if (hours >= 1) return `${hours} ${hours === 1 ? "hour" : "hours"} left`;
  const minutes = Math.floor(seconds / 60);
  if (minutes >= 1) return `${minutes} ${minutes === 1 ? "minute" : "minutes"} left`;
  return "less than a minute left";
}

/** What a trashed row is called on screen: a workspace or a chat by its title,
 *  never by the folder name the drive minted for it; anything else by its name. */
function trashName(row: TrashRow): string {
  return displayNameOf(row.item);
}

/** The name the server used for a restore. A restore onto a taken name comes back
 *  renamed, and the row that answers is what the person should go looking for. */
export function restoredName(answer: unknown): string | null {
  if (!answer || typeof answer !== "object") return null;
  const holder = "item" in answer ? (answer as { item: unknown }).item : answer;
  if (!holder || typeof holder !== "object") return null;
  const row = holder as { nameDisplay?: unknown; name?: unknown };
  if (typeof row.nameDisplay === "string" && row.nameDisplay) return row.nameDisplay;
  return typeof row.name === "string" && row.name ? row.name : null;
}

export interface TrashPageProps {
  driveId: string | undefined;
  /** True while the event stream is down, so reads fall back to polling. */
  streamDown?: boolean;
  /**
   * Choose the folder a "Restore to…" lands in. The folder picker is the browser's,
   * so the page supplies it; returning undefined cancels.
   */
  pickFolder?: () => Promise<string | undefined>;
  /** Facts the error copy needs that the envelope does not carry (the lease holder). */
  errorContext?: FilesErrorContext;
}

export function TrashPage({
  driveId,
  streamDown = false,
  pickFolder,
  errorContext = {},
}: TrashPageProps) {
  const trash = useTrash(driveId, { streamDown });
  const restore = useRestoreTrash();
  const purge = useTrashItem();
  const empty = useEmptyTrash();
  const qc = useQueryClient();
  // Every mutation names the version it believes it is changing. A restore names the
  // trashed row's; the sweep has no single row, so it names the drive root's — which is
  // why the root is read here and nowhere else on this page.
  const drive = useDrive({ streamDown });
  const root = useItem(driveId, drive.data?.rootId, { streamDown });

  const [focused, setFocused] = useState(0);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [confirmEmpty, setConfirmEmpty] = useState(false);

  // What an action came to is said in a toast, like everywhere else in the portal — not
  // as a line of text pushed into the page between the head and the rows. A success
  // clears itself; a refusal stays until it is closed, because it is the only place the
  // person learns the row is still in the trash.
  const { toasts, push, dismiss } = useToasts();
  const setNotice = useCallback(
    (text: string | null) => {
      if (text) push({ id: "trash-notice", tone: "success", message: text });
    },
    [push],
  );
  const setFailure = useCallback(
    (refusal: { title: string; detail?: string } | null) => {
      if (refusal) {
        push({
          id: "trash-failure",
          tone: "danger",
          title: refusal.title,
          message: refusal.detail,
          duration: null,
        });
      } else {
        dismiss("trash-failure");
      }
    },
    [push, dismiss],
  );
  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);

  const rows: TrashRow[] = useMemo(
    () =>
      (trash.data ?? []).flatMap((page) =>
        (page.entries ?? []).map((entry) => ({
          trashOpId: entry.trashOpId,
          item: entry.item,
          originalParentId: entry.originalParentId ?? null,
          originalPath: entry.originalPath ?? null,
          deletedAt: entry.deletedAt,
          timeLeftSeconds: entry.timeLeftSeconds,
        })),
      ),
    [trash.data],
  );

  // The trash pages exactly as a folder listing does: the marker pages are walked
  // until the budget runs out, and only then is the decision handed to the person.
  // A page of 50 with no pager left a drive's older deletions unreachable —
  // including, before the order was fixed, the one just made.
  const limits = useLimits();
  const pager = useCeilingPager(
    {
      hasNextPage: trash.hasNextPage === true,
      isFetchingNextPage: trash.isFetchingNextPage,
      fetchNextPage: trash.fetchNextPage,
    },
    limits.filesSoftThresholdRows,
    rows.length,
    driveId,
  );

  // The row the delete-forever confirmation is about. It is looked up rather than held, so a
  // listing that refreshes underneath an open prompt takes the question down with the row.
  const confirmingRow = useMemo(
    () => rows.find((row) => row.trashOpId === confirming) ?? null,
    [rows, confirming],
  );

  // A refusal about one row names that row's lease holder, read off the facet the
  // listing already carries — "your chat …" rather than "someone" when it is theirs.
  const report = useCallback(
    (error: unknown, row?: TrashRow) => {
      const copy = filesErrorCopy(error, { ...errorContext, ...leaseRefusalContext(row?.item) });
      setFailure({ title: copy.title, detail: copy.detail });
      setNotice(null);
    },
    [errorContext, setFailure, setNotice],
  );

  const doRestore = useCallback(
    async (row: TrashRow, parentId?: string) => {
      if (!driveId) return;
      setFailure(null);
      try {
        const answer = await restore.mutateAsync({
          driveId,
          opId: row.trashOpId,
          parentId,
          etag: row.item.etag,
        });
        const name = restoredName(answer);
        const original = row.item.nameDisplay || row.item.name;
        setNotice(
          name && name !== original
            ? `Restored as ${name} because that name was taken.`
            : `Restored ${trashName(row)}.`,
        );
      } catch (error) {
        report(error, row);
      }
    },
    [driveId, restore, report, setFailure, setNotice],
  );

  const doPurge = useCallback(
    async (row: TrashRow) => {
      if (!driveId) return;
      setFailure(null);
      setConfirming(null);
      try {
        await purge.mutateAsync({
          driveId,
          itemId: row.item.id,
          etag: row.item.etag,
          permanent: true,
        });
        setNotice(`Deleted ${trashName(row)} forever.`);
      } catch (error) {
        report(error, row);
      }
    },
    [driveId, purge, report, setFailure, setNotice],
  );

  const doEmpty = useCallback(async () => {
    if (!driveId) return;
    setFailure(null);
    setConfirmEmpty(false);
    try {
      const swept = await empty.mutateAsync({ driveId, etag: root.data?.etag });
      // Nothing is left to focus; the listing refresh comes from the cache policy.
      setFocused(0);
      // The sweep decides per deletion, so what it could not delete is still
      // here. Saying "the trash is empty" over those rows is the one thing the
      // notice must not do.
      setNotice(
        swept.skipped > 0
          ? `Removed ${swept.removed}. ${swept.skipped} ${
              swept.skipped === 1 ? "item stays" : "items stay"
            } in the trash because you cannot delete ${swept.skipped === 1 ? "it" : "them"}.`
          : "The trash is empty.",
      );
      await qc.invalidateQueries({ queryKey: keys.files.trashAll });
    } catch (error) {
      report(error);
    }
  }, [driveId, empty, qc, report, root.data?.etag, setFailure, setNotice]);

  const moveFocus = useCallback(
    (next: number) => {
      const clamped = Math.max(0, Math.min(rows.length - 1, next));
      setFocused(clamped);
      rowRefs.current[clamped]?.focus();
    },
    [rows.length],
  );

  const loading = trash.isPending && driveId !== undefined;

  return (
    // The shell's region already carries the name "Trash", and the masthead is the
    // page's one <h1>; this heading only marks where the trash listing starts.
    <div className="alk-files alk-files--trash">
      <header className="alk-files__head">
        <h2 className="alk-files__title">Trash</h2>
        <p className="alk-files__trail">
          Deleted items stay here for 30 days.
        </p>
        <div className="alk-files__toolbar-slot">
          <Button
            size="sm"
            fill="outline"
            disabled={rows.length === 0 || empty.isPending || root.data === undefined}
            onClick={() => setConfirmEmpty(true)}
          >
            Empty trash
          </Button>
        </div>
      </header>

      <ConfirmDialog
        open={confirmEmpty}
        onClose={() => setConfirmEmpty(false)}
        onConfirm={() => void doEmpty()}
        title="Empty the trash?"
        consequence="Everything in the trash is deleted and cannot be brought back."
        confirmLabel="Empty trash"
        cancelLabel="Keep them"
        tone="destructive"
        busy={empty.isPending}
      />
      <ConfirmDialog
        open={confirmingRow !== null}
        onClose={() => setConfirming(null)}
        onConfirm={() => confirmingRow && void doPurge(confirmingRow)}
        title={confirmingRow ? `Delete ${trashName(confirmingRow)} forever?` : ""}
        consequence="It cannot be brought back."
        confirmLabel="Delete forever"
        cancelLabel="Keep it"
        tone="destructive"
        busy={purge.isPending}
      />

      <ToastViewport toasts={toasts} onDismiss={dismiss} position="bottom-right" />

      {loading ? (
        <div className="alk-files__skeleton" aria-hidden="true" data-testid="trash-skeleton">
          <span className="alk-files__skeleton-row" />
          <span className="alk-files__skeleton-row" />
          <span className="alk-files__skeleton-row" />
        </div>
      ) : rows.length === 0 ? (
        <p className="alk-files__empty">
          Nothing is in the trash.
        </p>
      ) : (
        <div
          role="treegrid"
          aria-label="Trash"
          aria-rowcount={rows.length + 2}
          className="alk-files__grid"
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              moveFocus(focused + 1);
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              moveFocus(focused - 1);
            } else if (event.key === "Home") {
              event.preventDefault();
              moveFocus(0);
            } else if (event.key === "End") {
              event.preventDefault();
              moveFocus(rows.length - 1);
            }
          }}
        >
          <div role="row" aria-rowindex={1} className="alk-files__grid-head">
            <span role="columnheader">Name</span>
            <span role="columnheader">Original location</span>
            <span role="columnheader">Time left</span>
            <span role="columnheader">Actions</span>
          </div>
          {rows.map((row, index) => {
            const name = trashName(row);
            return (
              <div
                key={row.trashOpId}
                role="row"
                aria-rowindex={index + 2}
                aria-level={1}
                aria-posinset={index + 1}
                aria-setsize={rows.length}
                className="alk-files__row"
                tabIndex={index === focused ? 0 : -1}
                ref={(element) => {
                  rowRefs.current[index] = element;
                }}
                onFocus={() => setFocused(index)}
              >
                <span role="gridcell" className="alk-files__cell-name">
                  {name}
                </span>
                <span role="gridcell" className="alk-files__cell-path">
                  {row.originalPath === null ? "Original folder no longer exists" : trashLocation(row.originalPath)}
                </span>
                <span role="gridcell">{timeLeftLabel(row.timeLeftSeconds)}</span>
                <span role="gridcell" className="alk-files__cell-actions">
                  <Button
                    size="sm"
                    fill="outline"
                    onClick={() => void doRestore(row)}
                    aria-label={`Restore ${name}`}
                  >
                    Restore
                  </Button>
                  <Button
                    size="sm"
                    fill="outline"
                    aria-label={`Restore ${name} to another folder`}
                    onClick={() => {
                      void (async () => {
                        const parentId = await pickFolder?.();
                        if (parentId) await doRestore(row, parentId);
                      })();
                    }}
                  >
                    Restore to…
                  </Button>
                  <Button
                    size="sm"
                    variant="destructive"
                    fill="outline"
                    aria-label={`Delete ${name} forever`}
                    onClick={() => setConfirming(row.trashOpId)}
                  >
                    Delete forever
                  </Button>
                </span>
              </div>
            );
          })}
          <ThresholdFooter
            loaded={rows.length}
            total={pager.hasMore ? null : rows.length}
            exact={!pager.hasMore}
            hasMore={pager.hasMore}
            loadingAll={pager.loadingAll}
            loadMore={pager.loadMore}
            loadAll={pager.loadAll}
            cancelLoadAll={pager.cancelLoadAll}
            columnCount={4}
          />
        </div>
      )}
    </div>
  );
}

export default TrashPage;

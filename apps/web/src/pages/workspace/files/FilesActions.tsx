/**
 * One place every Files action is issued from.
 *
 * The context menu and the keyboard are two ways of asking for the same list of
 * things, so they are wired here rather than in the browser: the menu is built
 * from `contextMenuItems.ts` and the keys from `useFilesShortcuts`, and both end
 * up in `run()`. That is what keeps the two honest — a row the menu shows
 * disabled is an action the key cannot fire either, because both read the same
 * capabilities.
 *
 * Writes go through the hooks in `api/files.ts` and nothing else, and every one
 * that lands says so through `onOperation`. That seam is what feeds the undo
 * stack: the page never guesses what happened from what it asked for — it is
 * told, with the operation the SERVER named, which is the only handle
 * `POST /operations/{id}/undo` accepts.
 *
 * Restore stays a page callback: the trash lists *deletions*, so the inverse of
 * a trash is addressed by a trash-op id, which a node-addressed action never
 * holds.
 */

import { useCallback, useEffect, useMemo, useReducer, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { Button, ContextMenu, useContextMenu, type ContextMenuItem } from "@alkera/ui";

import { keys } from "@/api/keys";
import { FILES_BULK_BATCH_ITEMS, FILES_BULK_INLINE_ITEMS, useLimits } from "@/lib/limits";

import {
  etagVersion,
  isOperation,
  useBulkTrash,
  useCopyItem,
  useDuplicateItem,
  useForceRelease,
  useLeases,
  useMoveItem,
  useReleaseLease,
  useRequestRelease,
  useTrashItem,
  type BulkItemResult,
  type BulkTrashResult,
  type Item,
  type ItemOrOperation,
  type Operation,
} from "@/api/files";
import type { Crumb } from "./Breadcrumbs";
import { isChatFolder, isTemplateFolder } from "@/lib/files/chatFolder";
import { displayNameOf } from "@/lib/files/columns";
import {
  buildContextMenuItems,
  IN_LEASED_FOLDER,
  LEASED_HERE,
  menuVerdict,
  type MenuActionId,
} from "./contextMenuItems";
import { leasedFolderRefusal } from "./dropHandlers";
import {
  batchRowErrorCopy,
  filesErrorCopy,
  type FilesErrorContext,
  type FilesErrorCopy,
} from "@/lib/files/errors";
import { contentUrl, downloadItem } from "@/lib/files/download";
import { copyLinkTo } from "@/lib/files/links";
import { leaseRefusalContext } from "./useLeaseFacet";
import { newTabHrefOf } from "@/lib/files/openTarget";
import { CopyToDialog } from "./CopyToDialog";
import { FileVersionsDialog } from "./FileVersionsDialog";
import { MoveToDialog } from "./MoveToDialog";
import { SaveAsTemplateDialog } from "./SaveAsTemplateDialog";
import { ForceReleaseDialog, forceReleaseNeedsReason } from "./ForceReleaseDialog";
import { ShareDialog } from "./ShareDialog";
// Imported for the registration it performs: this is the Files surface that
// mounts the share dialog, so a template row's link targets must be known by
// the time anything here can open it.
import "./templateLinks";
import {
  clipboardReducer,
  emptyClipboard,
  pastePlan,
  type ClipboardState,
  type PastePlan,
} from "./state/clipboard";
import type { Platform } from "./state/shortcuts";
import { useFilesShortcuts } from "./useFilesShortcuts";

/** What a landed write reports back.
 *
 *  `operationId` is present only when the server named an operation — a queued
 *  move (202), every copy, and every trash (200, because trashing runs as the
 *  operation its undo inverts). The purge is the one destructive write that
 *  names none (204), and a report without one is deliberately NOT undoable
 *  rather than undoable against an id the browser invented. */
export interface FilesOperationReport {
  kind: "trash" | "move" | "copy";
  driveId: string;
  /** The operation the server named, when it named one. */
  operationId?: string;
  /** From the operation's `undoableUntil`, when there is an operation. */
  undoableUntil?: string;
  /** What the operation says about itself: whether the server recorded an
   *  inverse it will apply. A copy records none, so a copy is not history. */
  undoable?: boolean;
  /** Product copy for the toast, e.g. `Copied 3 items`. */
  label: string;
  /** The rows the write acted on. */
  items: readonly Item[];
  /** The node exactly as the answer returned it, when the answer was the node
   *  rather than an operation. It carries the version the write produced, which
   *  is the only one an inverse the browser issues itself can be fenced on — the
   *  version the caller sent is spent, because the write bumped it. */
  node?: Item;
  /** The folder the write took the rows out of, so an inverse knows where to put
   *  them back. A paste happens in a folder the cut rows are no longer listed in,
   *  so it cannot be read off a row: the clipboard carries it. */
  fromParentId?: string;
}

/** Where the browser sends a person to fetch the bytes. */
/** The default answer to "is this row in a folder a machine is holding": a
 *  surface that does not say is one where no row is. */
const NOT_HELD = (): boolean => false;

/** The actions that write INTO the listed folder, refused while a machine
 *  holds it. Duplicate lands beside its source, which is this folder. */
const WRITES_INTO_LISTING: ReadonlySet<MenuActionId> = new Set<MenuActionId>([
  "paste",
  "duplicate",
  "new-folder",
  "new-notebook",
  "upload-files",
  "upload-folder",
]);

/** The actions the menu refuses on the caller's capabilities, and which every
 *  other road to them — a keystroke, a toolbar button — therefore asks the menu
 *  about first. Enter and F2 opened the rename editor for a viewer the menu had
 *  already told "You can view this, not rename it", and Upload files opened a
 *  picker into a folder they could not add to; both ended in a refusal the
 *  server gave after the work was done. */
const GATED_BY_MENU: ReadonlySet<MenuActionId> = new Set<MenuActionId>([
  "rename",
  "share",
  "move-to",
  "cut",
  "paste",
  "duplicate",
  "download",
  "new-folder",
  "new-notebook",
  "upload-files",
  "upload-folder",
  "trash",
  "delete-forever",
]);

/** The actions that change a ROW, refused while the row's folder is held. */
const WRITES_ON_ROW: ReadonlySet<MenuActionId> = new Set<MenuActionId>([
  "rename",
  "cut",
  "move-to",
  "duplicate",
  "trash",
]);

/** What the wired page hands its children. */
export interface FilesActionsApi {
  /** Spread on the element that owns the rows. Carries the menu's two openers
   *  (right-click, Shift+F10) AND the keyboard table, already composed, so a
   *  caller cannot wire one and silently lose the other. */
  triggerProps: {
    onContextMenu: ReturnType<typeof useContextMenu>["triggerProps"]["onContextMenu"];
    onKeyDown: ReturnType<typeof useFilesShortcuts>["onKeyDown"];
  };
  onKeyDown: ReturnType<typeof useFilesShortcuts>["onKeyDown"];
  clipboard: ClipboardState;
  /** Ask for the menu at a point — a row's own kebab button, say. */
  openMenuAt: ReturnType<typeof useContextMenu>["openAt"];
  /** The menu rows for the current selection. Handed out so a surface that
   *  offers some of the same actions outside the menu — the selection bar under
   *  the listing — reads THIS list rather than deciding a second time what may
   *  run. */
  menuItems: readonly ContextMenuItem[];
  run: (action: MenuActionId, targets?: readonly Item[]) => void;
  /** Move these rows into a folder — the same write, report and refusal copy
   *  the Move to… dialog uses, offered to a drop so the two never differ. The
   *  `destination` row, when the caller has it, names the place in the toast. */
  moveInto: (items: readonly Item[], parentId: string, destination?: Item) => void;
  /** Say why something was not done, in the same inline line a refused write
   *  uses. For a refusal decided before any request was sent. */
  refuse: (title: string, detail?: string) => void;
}

/** The render prop this component calls with its wired api.
 *
 *  Named so a surface that mounts the actions somewhere other than the Files
 *  page — the chat's file explorer among them — can type its own body against
 *  the contract rather than restating it. */
export type FilesActionsChildren = (api: FilesActionsApi) => ReactNode;

/** The clipboard the api hands back, and the plan a paste stages, re-exported
 *  here so a surface holding a `FilesActionsApi` types against the one module
 *  it already imports. */
export type { ClipboardState, PastePlan } from "./state/clipboard";

export interface FilesActionsProps {
  driveId: string | undefined;
  currentFolderId: string | undefined;
  /** What the folder on screen is called, for the picker's first crumb. */
  currentFolderName?: string;
  /** The ancestors of the folder on screen, outermost first, so the picker's
   *  trail climbs. Read by identity: a new array every render re-seeds it. */
  currentTrail?: readonly Crumb[];
  /** The rows the menu and the keys act on. */
  selection: readonly Item[];
  platform?: Platform;
  /** Whether the caller may add to the listed folder, read off the folder's
   *  own capabilities. Required, so no caller is granted a write by omission. */
  canWriteHere: boolean;
  /** A workspace machine is holding the listed folder. Nothing is written into
   *  it while that lasts: no create, no upload, no paste, no duplicate. */
  leasedHere?: boolean;
  /** Whether a row sits in a folder a machine is holding: such a row is not
   *  renamed, cut, moved, duplicated or trashed while that lasts. Asked per
   *  row so a feed's rows answer for their own folders. Absent, no row is. */
  isHeld?: (item: Item) => boolean;
  canForceRelease?: boolean;
  inTrash?: boolean;
  /** Modals aside, the page decides what "open" means, and it reads the one
   *  answer every gesture reads (`openTargetOf`). */
  onOpen?: (item: Item) => void;
  /** List a chat's, a workspace's or a template's own files rather than open
   *  its page. Everything else in the drive already is its own contents. */
  onViewFiles?: (item: Item) => void;
  /** Open the page of a folder-object whose Open lists its files (a
   *  workspace), at the address the server named on its facet. */
  onOpenPage?: (item: Item) => void;
  onOpenParent?: () => void;
  onRename?: (item: Item) => void;
  onNewFolder?: () => void;
  /** Create a notebook in the listed folder. Wired, the menu offers it. */
  onNewNotebook?: () => void;
  onUploadFiles?: () => void;
  onUploadFolder?: () => void;
  onSearch?: (scope: "folder" | "drive") => void;
  onQuickLook?: () => void;
  onDetails?: (item: Item) => void;
  /** Start a brand-new chat from the row's node. The page routes it, so the
   *  reader lands in the chat with its rail and composer mounted rather than in
   *  Files watching a request. Only a chat template ever reaches this. */
  onStartChat?: (item: Item) => void;
  /** Take over "Save as template…" — a surface that wants its own dialog, or
   *  its own destination. Left out, this component opens the dialog itself. */
  onSaveAsTemplate?: (item: Item) => void;
  /** Where a new chat from a template is opened. Falls back to
   *  {@link FilesActionsProps.onStartChat}, which is the same routing. */
  onNewChatFromTemplate?: (item: Item) => void;
  onUndo?: () => void;
  onRedo?: () => void;
  onRelease?: (item: Item) => void;
  onRestore?: (items: readonly Item[]) => void;
  /** Every write that lands, with the operation the server named. */
  onOperation?: (report: FilesOperationReport) => void;
  /** The holder's own instance id when this surface knows it. A release is
   *  fenced on (epoch, instance) and `LeaseRow` carries no instance, so the
   *  machine the lease was taken on is the documented fallback. */
  holderInstanceId?: string;
  /** Override the copy route — a page that wants to stage the paste itself. */
  onCopyInto?: (plan: PastePlan) => void;
  onDuplicate?: (items: readonly Item[]) => void;
  /** Overridable so a test can watch the download without a jsdom navigation. */
  onDownload?: (url: string, item: Item) => void;
  children: FilesActionsChildren;
}

/** The operation a node-scoped PATCH answered, when it answered one rather than
 *  the changed node. */
function queuedOperation(result: ItemOrOperation): Operation | undefined {
  return isOperation(result) ? result : undefined;
}

/**
 * How a selection is split so every batch is above the server's inline ceiling
 * and at or below its cap.
 *
 * Split evenly rather than filled from the front, because a trailing chunk is
 * the one that lands under the ceiling: 1,050 rows taken a thousand at a time
 * is `[1000, 50]`, and the server applies the 50 in the request — a different
 * answer shape, reported differently, for no reason a reader could see. Even
 * chunks cannot produce one: past a hundred rows the smallest even split of a
 * batch is half of something over a thousand.
 */
export function batchSizes(count: number): number[] {
  const batches = Math.ceil(count / FILES_BULK_BATCH_ITEMS);
  if (batches === 0) return [];
  const size = Math.ceil(count / batches);
  const sizes: number[] = [];
  for (let left = count; left > 0; left -= size) sizes.push(Math.min(size, left));
  return sizes;
}

/** What a batch did, read off whichever shape the route answered with.
 *
 *  A queued batch has not been applied yet, so its rows are what was ASKED —
 *  the per-item results land on the operation row as the runner walks it, which
 *  is not something the answer to this request can carry. An inline batch has
 *  run, and its rows say which half of the selection actually moved. */
export function batchOutcome(
  result: BulkTrashResult,
  batch: readonly Item[],
): { moved: readonly Item[]; refused: readonly BulkItemResult[] } {
  if ("id" in result) return { moved: batch, refused: [] };
  const refused = result.responses.filter((row) => row.status >= 300);
  const out = new Set(refused.map((row) => row.id));
  return { moved: batch.filter((_, at) => !out.has(batchHandle(at))), refused };
}

/** The client's own correlation handle for a row's place in its batch. The
 *  results come back carrying it, so an answer is matched to the row it is
 *  about without relying on the order they arrive in. */
function batchHandle(at: number): string {
  return `t${at}`;
}

/** The operation a batch was queued as, or nothing when it ran inline.
 *
 *  Passed on as the server wrote it, `undoable` and all: whether a batch can be
 *  taken back is the server's answer, not a rule restated here. */
function batchOperation(result: BulkTrashResult): Operation | undefined {
  return "id" in result ? result : undefined;
}

/** The changed node a node-scoped PATCH answered, when it ran the write inline
 *  instead of naming an operation. Its etag is the one an inverse must send. */
function landedNode(result: ItemOrOperation): Item | undefined {
  return isOperation(result) ? undefined : result;
}

/** The " to <place>" the move toast adds, or nothing.
 *
 *  The place is read off the ANSWER, never off the id that was sent: the server
 *  decides where a node lands — a chat resolves to the directory its agent runs
 *  in — so a toast that echoed the request could name a folder the file is not
 *  in. The destination row is only used to spell the name of the parent the
 *  answer reported; when they are not the same node, or the move was queued and
 *  named no node at all, the toast says what it knows and stops there.
 */
function landedIn(result: ItemOrOperation, destination: Item | undefined): string {
  const landed = landedNode(result);
  if (!landed || !destination || landed.parentId !== destination.id) return "";
  return ` to ${displayNameOf(destination)}`;
}

/** What the refusal copy needs that the envelope never carries: who holds the lease,
 *  and the verb to name in the legal-hold sentence. The lease facet rides the item the
 *  listing already delivered, so naming the holder costs no request. */
function why(item: Item | undefined, verb: string): FilesErrorContext {
  return { action: verb, ...leaseRefusalContext(item) };
}

/** What a toast names: the one row, or how many there were. */
export function subjectLabel(items: readonly Item[]): string {
  const first = items[0];
  if (items.length === 1 && first) return displayNameOf(first);
  return `${items.length} items`;
}

export function FilesActions(props: FilesActionsProps) {
  const {
    driveId,
    currentFolderId,
    currentFolderName,
    currentTrail,
    selection,
    platform = "other",
    canWriteHere,
    leasedHere = false,
    isHeld = NOT_HELD,
    canForceRelease = false,
    inTrash = false,
    children,
  } = props;
  const canNewNotebook = props.onNewNotebook !== undefined;

  const [clipboard, dispatchClipboard] = useReducer(clipboardReducer, emptyClipboard);
  const [moveOpen, setMoveOpen] = useState(false);
  const [copyToOpen, setCopyToOpen] = useState(false);
  // The node the share dialog is open on, or null. It is an id rather than a
  // flag because the dialog reads the node itself — the row the menu was opened
  // over is a snapshot, and a share is decided against the node's live version.
  // The name rides along because the node's own name is not always what the
  // reader knows it by: a chat is stored as `<uuid>.alkerachat`, and a dialog
  // titled with that asks somebody to share a 36-character id on trust.
  const [sharing, setSharing] = useState<{ nodeId: string; subjectName: string } | null>(null);
  // True once a link this menu copied is actually on the clipboard.
  const [linkCopied, setLinkCopied] = useState(false);
  // The chat the save-as-template dialog is open over, or null. The ROW and not
  // an id: the dialog titles the template after the chat, and the row is the
  // only thing that carries the title a person has been reading.
  const [savingTemplate, setSavingTemplate] = useState<Item | null>(null);
  // The file whose history is open, or null. An id and a name, like the share
  // dialog and for the same reason: the history is read against the node's live
  // version, and the row the menu was opened over is a snapshot of it.
  const [versionsOf, setVersionsOf] = useState<{ nodeId: string; subjectName: string } | null>(
    null,
  );
  const [refusal, setRefusal] = useState<FilesErrorCopy | null>(null);
  // The row a box's lease is being forced off, while its reason is asked for.
  const [forcing, setForcing] = useState<Item | null>(null);
  const menu = useContextMenu();

  const move = useMoveItem();
  const trash = useTrashItem();
  const bulkTrash = useBulkTrash();
  const copy = useCopyItem();
  const duplicate = useDuplicateItem();
  const requestRelease = useRequestRelease();
  const forceRelease = useForceRelease();
  const releaseLease = useReleaseLease();
  // My own leases carry the epoch a release must be fenced against, and the
  // facet deliberately does not. Read only when a selected row actually claims
  // a lease, so a selection of ordinary files costs no request.
  const leased = selection.some((row) => row.lease != null);
  const leaseRows = useLeases(leased ? driveId : undefined).data;

  const { onOperation } = props;

  /** Say one landed write happened. Split out so a 202 and a 200 on the same
   *  route take the same path and differ only in whether an id rides along. */
  const report = useCallback(
    (
      kind: FilesOperationReport["kind"],
      label: string,
      items: readonly Item[],
      operation?: Operation,
      node?: Item,
      fromParentId?: string,
    ) => {
      onOperation?.({
        kind,
        driveId: driveId ?? "",
        operationId: operation?.id,
        undoableUntil: operation?.undoableUntil ?? undefined,
        undoable: operation?.undoable ?? false,
        label,
        items,
        node,
        fromParentId,
      });
    },
    [onOperation, driveId],
  );

  /** The options every write in this module passes to `mutate`.
   *
   *  A refusal is the whole point of this seam: a lease, a legal hold, a stale
   *  precondition and the quota all answer 4xx and change nothing, so a write with
   *  no `onError` leaves the row where it was and says nothing — which reads as a
   *  broken product rather than as a refusal. The copy comes from the one table in
   *  `errors.ts` so this surface and the trash cannot disagree about what a code
   *  means, and the lease facet the page already read names the holder.
   *
   *  A landed write clears it: the sentence is about the attempt, not the row. */
  const settle = useCallback(
    <T,>(
      context: FilesErrorContext,
      onSuccess?: (value: T) => void,
    ): { onSuccess: (value: T) => void; onError: (error: unknown) => void } => ({
      onSuccess: (value: T) => {
        setRefusal(null);
        onSuccess?.(value);
      },
      onError: (error: unknown) => setRefusal(filesErrorCopy(error, context)),
    }),
    [],
  );

  const qc = useQueryClient();

  // A refusal is the only statement that the write did not happen, so it waits —
  // but it does not wait for ever. It is drawn over the page, so left up it would
  // cover the selection bar, the undo offer and the paging footer until the next
  // write happened to land.
  const limits = useLimits();
  useEffect(() => {
    if (refusal === null) return;
    const timer = setTimeout(() => setRefusal(null), limits.filesRefusalMs);
    return () => clearTimeout(timer);
  }, [refusal, limits.filesRefusalMs]);

  const refuse = useCallback((title: string, detail?: string) => {
    setRefusal({
      code: "files.refused_before_request",
      title,
      retryable: false,
      ...(detail ? { detail } : {}),
    });
  }, []);

  const run = useCallback(
    (action: MenuActionId, over?: readonly Item[]) => {
      const targets = over ?? selection;
      const first = targets[0];
      const write = (item: Item) => ({ driveId: driveId ?? "", itemId: item.id, etag: item.etag });
      // The copy notice is about the last thing asked for, so anything else
      // asked for afterwards clears it.
      setLinkCopied(false);
      // A folder a machine is holding is read-only here, whichever way the
      // write was asked for: the menu says so on its rows, and this is where a
      // key, a toolbar button or a caller reaching `run` directly is refused,
      // in the same line a refused write uses and before any request.
      if (WRITES_INTO_LISTING.has(action) && leasedHere) {
        refuse(LEASED_HERE);
        return;
      }
      if (WRITES_ON_ROW.has(action) && targets.some(isHeld)) {
        refuse(IN_LEASED_FOLDER);
        return;
      }
      if (GATED_BY_MENU.has(action)) {
        const verdict = menuVerdict(
          {
            platform,
            targets,
            currentFolderId,
            canWriteHere,
            leasedHere,
            isHeld,
            canForceRelease,
            canNewNotebook,
            inTrash,
            clipboard,
          },
          action,
        );
        // Not in the menu: the action does not apply to this selection, and a
        // key that asks for it anyway does nothing rather than picking a row.
        if (verdict === null) return;
        if (verdict !== undefined) {
          refuse(verdict);
          return;
        }
      }
      switch (action) {
        case "open":
          if (first) props.onOpen?.(first);
          return;
        case "view-files":
          if (first) props.onViewFiles?.(first);
          return;
        case "open-page":
          if (first) props.onOpenPage?.(first);
          return;
        case "open-new-tab":
          // Where a double-click goes, in a tab of its own: a chat's node is a
          // real folder, and its listing is Browse files, a different entry.
          if (first) window.open(newTabHrefOf(first), "_blank", "noopener");
          return;
        case "rename":
          if (first) props.onRename?.(first);
          return;
        case "move-to":
          if (targets.length > 0) setMoveOpen(true);
          return;
        case "copy-to":
          if (targets.length > 0) setCopyToOpen(true);
          return;
        case "share":
          // One node at a time: a share is a conversation about ONE thing,
          // and the permissions routes are node-addressed.
          if (first) setSharing({ nodeId: first.id, subjectName: displayNameOf(first) });
          return;
        case "copy-link":
          // One row, one link. The outcome is reported only once the clipboard
          // write has settled: a browser can refuse it, and saying "copied"
          // before it answers is how a person pastes the previous link instead.
          if (first) {
            void copyLinkTo(first).then((outcome) => {
              setLinkCopied(outcome === "copied");
              setRefusal(
                outcome === "copied"
                  ? null
                  : {
                      code: "files.link_not_copied",
                      title: "The link was not copied.",
                      retryable: true,
                    },
              );
            });
          }
          return;
        case "copy":
        case "cut":
          if (targets.length > 0 && currentFolderId !== undefined) {
            dispatchClipboard({
              type: action,
              items: targets.map((item) => ({
                id: item.id,
                etag: item.etag,
                name: item.nameDisplay || item.name,
              })),
              sourceFolderId: currentFolderId,
            });
          }
          return;
        case "paste": {
          if (currentFolderId === undefined) return;
          const plan = pastePlan(clipboard, currentFolderId);
          if (plan === null) return;
          // A paste happens in a folder the cut rows are no longer listed in, so
          // the clipboard carries what the write needs — the id, the etag the
          // item was cut at, and its name — and a row is looked up but never
          // required: after a walk to another folder the selection cannot hold it.
          for (const entry of plan.items) {
            const row = selection.find((item) => item.id === entry.id);
            const subjects = row ? [row] : [];
            const named = entry.name;
            if (plan.operation === "move") {
              move.mutate(
                {
                  driveId: driveId ?? "",
                  itemId: entry.id,
                  etag: entry.etag,
                  parentId: plan.targetFolderId,
                },
                settle(why(row, "moved"), (result) =>
                  report(
                    "move",
                    `Moved ${named}`,
                    subjects,
                    queuedOperation(result),
                    landedNode(result),
                    plan.sourceFolderId,
                  ),
                ),
              );
            } else if (props.onCopyInto) {
              props.onCopyInto(plan);
              break;
            } else {
              copy.mutate(
                { driveId: driveId ?? "", itemId: entry.id, parentId: plan.targetFolderId },
                settle(why(row, "copied"), (operation) =>
                  report("copy", `Copied ${named}`, subjects, operation),
                ),
              );
            }
          }
          dispatchClipboard({ type: "paste", targetFolderId: currentFolderId });
          return;
        }
        case "duplicate":
          if (targets.length === 0) return;
          if (props.onDuplicate) {
            props.onDuplicate(targets);
            return;
          }
          // A duplicate is a copy into the folder the row already sits in; the
          // server's `rename` conflict behaviour picks the "copy 2" name, so the
          // client never spells one.
          for (const item of targets) {
            copy.mutate(
              { driveId: driveId ?? "", itemId: item.id, parentId: item.parentId ?? "" },
              settle(why(item, "copied"), (operation) =>
                report("copy", `Copied ${subjectLabel([item])}`, [item], operation),
              ),
            );
          }
          return;
        case "download":
          if (first && driveId !== undefined) {
            const url = contentUrl(driveId, first.id);
            (props.onDownload ?? downloadItem)(url, first);
          }
          return;
        case "new-folder":
          props.onNewFolder?.();
          return;
        case "new-notebook":
          props.onNewNotebook?.();
          return;
        case "upload-files":
          props.onUploadFiles?.();
          return;
        case "upload-folder":
          props.onUploadFolder?.();
          return;
        case "release": {
          if (!first) return;
          if (props.onRelease) {
            props.onRelease(first);
            return;
          }
          // A release is fenced on (epoch, instance). The epoch lives on my own
          // lease row, never on the facet; the instance is this surface's when
          // it has one and the machine the lease was taken on otherwise.
          const held = leaseRows?.find((row) => row.nodeId === first.id);
          if (!held) return;
          releaseLease.mutate(
            {
              ...write(first),
              epoch: held.epoch,
              instanceId: props.holderInstanceId ?? held.machine,
            },
            settle(why(first, "released")),
          );
          return;
        }
        case "request-release":
          if (first) requestRelease.mutate(write(first), settle(why(first, "released")));
          return;
        case "force-release":
          if (!first) return;
          // A box's lease is cut off only with a reason, asked for first.
          if (forceReleaseNeedsReason(first.lease?.purpose)) setForcing(first);
          else forceRelease.mutate(write(first), settle(why(first, "released")));
          return;
        case "versions":
          // Only a file has one, and the menu already says so; the guard is here
          // because a key or a caller can reach `run` directly.
          if (first && first.kind === "file") {
            setVersionsOf({ nodeId: first.id, subjectName: displayNameOf(first) });
          }
          return;
        case "details":
          if (first) props.onDetails?.(first);
          return;
        case "save-as-template":
          // Only a chat can be saved as one, and the menu already says so; the
          // guard is here because a key or a caller can reach `run` directly.
          if (first && isChatFolder(first)) {
            if (props.onSaveAsTemplate) props.onSaveAsTemplate(first);
            else setSavingTemplate(first);
          }
          return;
        case "new-chat-from-template":
          // The chat is created by the surface that owns chats, not here: this
          // hands the template's node id over and lets /chat create it.
          if (first && isTemplateFolder(first)) {
            (props.onNewChatFromTemplate ?? props.onStartChat)?.(first);
          }
          return;
        case "trash": {
          if (targets.length === 0) return;
          // Past the server's inline ceiling a selection is work, not a handful
          // of requests: `Select all` can arm every row of a folder, and one
          // DELETE per row is thousands of requests taking a rate-limit slot
          // each. The batch route carries up to `FILES_BULK_BATCH_ITEMS` and
          // answers a longer one as a queued operation, which the server tracks
          // and can be asked to cancel — nothing here offers that yet. Below the
          // ceiling the per-row path stays, because there each row is its own
          // operation and undoing one leaves the others trashed.
          // A batch item fences on the etag as the integer counter it is, and a
          // row whose etag is not one cannot be sent that way. That is the
          // per-row route's header format, so such a selection falls back to it
          // rather than going out unfenced.
          const fenceable = targets.every((row) => etagVersion(row.etag) !== null);
          if (fenceable && targets.length > FILES_BULK_INLINE_ITEMS) {
            let from = 0;
            for (const size of batchSizes(targets.length)) {
              const batch = targets.slice(from, from + size);
              from += size;
              bulkTrash.mutate(
                {
                  driveId: driveId ?? "",
                  items: batch.map((row) => ({ itemId: row.id, etag: row.etag })),
                },
                settle(why(first, "moved to the trash"), (result) => {
                  const { moved, refused } = batchOutcome(result, batch);
                  // An item's refusal is its own, so the batch around it
                  // committed: the reader is told which half is which, with the
                  // reason the server gave the first row that was turned down.
                  const worst = refused[0];
                  if (worst !== undefined) {
                    setRefusal(batchRowErrorCopy(worst, why(first, "moved to the trash")));
                  }
                  if (moved.length === 0) return;
                  // A queued batch has been accepted, not applied: the runner
                  // walks it afterwards and its per-row answers land on the
                  // operation row, not on this response. Saying "moved" here
                  // would be the client reporting an outcome it has not seen.
                  const label =
                    "id" in result
                      ? `Moving ${subjectLabel(moved)} to trash`
                      : `Moved ${subjectLabel(moved)} to trash${
                          refused.length === 0 ? "" : ` · ${refused.length} could not be moved`
                        }`;
                  // Reported with the operation exactly as the server named it,
                  // `undoable` and all: a batch is filed under its own kind with
                  // no inverse recorded, so the server answers `undoable: false`
                  // and `undoStep` declines it. The client decides nothing here —
                  // the day a batch does record an inverse, the undo appears
                  // because the server said so. An inline batch names no
                  // operation at all and is not history either way.
                  report("trash", label, moved, batchOperation(result));
                }),
              );
            }
            return;
          }
          for (const item of targets) {
            // Each row is trashed as its own operation and reported with the id
            // the server minted for it, so undoing one does not drag the rest
            // back — and reports only once the write actually landed.
            trash.mutate(
              write(item),
              settle(why(item, "moved to the trash"), (operation) =>
                report(
                  "trash",
                  `Moved ${subjectLabel([item])} to trash`,
                  [item],
                  operation ?? undefined,
                ),
              ),
            );
          }
          return;
        }
        case "restore":
          if (targets.length > 0) props.onRestore?.(targets);
          return;
        case "delete-forever":
          for (const item of targets) {
            trash.mutate({ ...write(item), permanent: true }, settle(why(item, "deleted")));
          }
          return;
      }
    },
    [
      selection,
      driveId,
      currentFolderId,
      clipboard,
      leasedHere,
      isHeld,
      platform,
      canWriteHere,
      canForceRelease,
      canNewNotebook,
      inTrash,
      refuse,
      move,
      trash,
      bulkTrash,
      copy,
      requestRelease,
      forceRelease,
      releaseLease,
      leaseRows,
      report,
      settle,
      props,
    ],
  );

  const items = useMemo(
    () =>
      buildContextMenuItems({
        platform,
        targets: selection,
        currentFolderId,
        canWriteHere,
        leasedHere,
        isHeld,
        canForceRelease,
        canNewNotebook,
        inTrash,
        clipboard,
        onAction: run,
      }),
    [
      platform,
      selection,
      currentFolderId,
      canWriteHere,
      leasedHere,
      isHeld,
      canForceRelease,
      canNewNotebook,
      inTrash,
      clipboard,
      run,
    ],
  );

  const { onKeyDown } = useFilesShortcuts({
    platform,
    enabled:
      !moveOpen &&
      sharing === null &&
      savingTemplate === null &&
      versionsOf === null &&
      forcing === null,
    handlers: useMemo(
      () => ({
        open: () => run("open"),
        "open-parent": () => props.onOpenParent?.(),
        rename: () => run("rename"),
        share: () => run("share"),
        trash: () => run("trash"),
        copy: () => run("copy"),
        cut: () => run("cut"),
        paste: () => run("paste"),
        undo: () => props.onUndo?.(),
        redo: () => props.onRedo?.(),
        "new-folder": () => run("new-folder"),
        "search-folder": () => props.onSearch?.("folder"),
        "search-drive": () => props.onSearch?.("drive"),
        "quick-look": () => props.onQuickLook?.(),
      }),
      [run, props],
    ),
  });

  /** Whether the folder a picker or a drop named is one a machine is holding.
   *  The picker hands back the row it walked when it has one, and the id alone
   *  when the destination is the folder it was listing; that folder's read is
   *  in the cache, put there by the walk that listed it. */
  const heldFolder = useCallback(
    (id: string, known?: Item): boolean => {
      const folder = known ?? qc.getQueryData<Item>(keys.files.item(id));
      return folder !== undefined && isHeld(folder);
    },
    [qc, isHeld],
  );

  const moveInto = useCallback(
    (items: readonly Item[], parentId: string, destination?: Item) => {
      // Into a held folder, or out of one: both are writes the lease refuses.
      if (heldFolder(parentId, destination)) {
        refuse(leasedFolderRefusal(destination ? displayNameOf(destination) : "That folder"));
        return;
      }
      if (items.some(isHeld)) {
        refuse(IN_LEASED_FOLDER);
        return;
      }
      for (const item of items) {
        move.mutate(
          { driveId: driveId ?? "", itemId: item.id, etag: item.etag, parentId },
          settle(why(item, "moved"), (result) =>
            report(
              "move",
              `Moved ${subjectLabel([item])}${landedIn(result, destination)}`,
              [item],
              queuedOperation(result),
              landedNode(result),
              item.parentId ?? undefined,
            ),
          ),
        );
      }
    },
    [driveId, move, report, settle, heldFolder, isHeld, refuse],
  );

  const label =
    selection.length === 1 && selection[0]
      ? `Actions for ${displayNameOf(selection[0])}`
      : "Actions";

  // Shift+F10 opens the menu, everything else is the keyboard table; one handler
  // so the host element can never carry half of the contract.
  const composedKeyDown = useCallback<typeof onKeyDown>(
    (event) => {
      menu.triggerProps.onKeyDown(event);
      if (!event.defaultPrevented) onKeyDown(event);
    },
    [menu.triggerProps, onKeyDown],
  );

  return (
    <>
      {children({
        triggerProps: {
          onContextMenu: menu.triggerProps.onContextMenu,
          onKeyDown: composedKeyDown,
        },
        onKeyDown: composedKeyDown,
        clipboard,
        openMenuAt: menu.openAt,
        menuItems: items,
        run,
        moveInto,
        refuse,
      })}
      {refusal ? (
        <div className="alk-files__refusal" role="alert" data-code={refusal.code}>
          <strong>{refusal.title}</strong>
          {refusal.detail ? <span>{refusal.detail}</span> : null}
          <Button
            variant="secondary"
            fill="ghost"
            size="sm"
            className="alk-files__refusal-dismiss"
            onClick={() => setRefusal(null)}
          >
            Dismiss
          </Button>
        </div>
      ) : null}
      {linkCopied ? (
        <p className="alk-files__notice" role="status">
          Link copied
        </p>
      ) : null}
      <ContextMenu {...menu.menuProps} items={items} label={label} />
      <ShareDialog
        driveId={driveId}
        nodeId={sharing?.nodeId}
        open={sharing !== null}
        onClose={() => setSharing(null)}
        {...(sharing ? { subjectName: sharing.subjectName } : {})}
      />
      <CopyToDialog
        open={copyToOpen}
        driveId={driveId}
        startFolderId={currentFolderId}
        {...(currentFolderName ? { startLabel: currentFolderName } : {})}
        {...(currentTrail ? { startTrail: currentTrail } : {})}
        count={selection.length}
        {...(platform ? { platform } : {})}
        onCancel={() => setCopyToOpen(false)}
        onConfirm={(destinationId, destination) => {
          // A copy reads its source and writes its destination: only the
          // destination can be a folder a machine is holding.
          if (heldFolder(destinationId, destination)) {
            refuse(leasedFolderRefusal(destination ? displayNameOf(destination) : "That folder"));
            setCopyToOpen(false);
            return;
          }
          for (const item of selection) {
            duplicate.mutate(
              { driveId: driveId ?? "", itemId: item.id, destinationId },
              settle(why(item, "copied"), (made) =>
                report(
                  "copy",
                  `Copied ${subjectLabel([item])}${
                    destination ? ` to ${displayNameOf(destination)}` : ""
                  }`,
                  [item],
                  undefined,
                  made.item,
                ),
              ),
            );
          }
          setCopyToOpen(false);
        }}
      />
      <SaveAsTemplateDialog
        open={savingTemplate !== null}
        chat={savingTemplate ?? undefined}
        onClose={() => setSavingTemplate(null)}
      />
      <ForceReleaseDialog
        open={forcing !== null}
        subjectName={forcing ? displayNameOf(forcing) : ""}
        busy={forceRelease.isPending}
        onClose={() => setForcing(null)}
        onConfirm={(reason) => {
          const item = forcing;
          setForcing(null);
          if (!item) return;
          forceRelease.mutate(
            { driveId: driveId ?? "", itemId: item.id, etag: item.etag, reason },
            settle(why(item, "released")),
          );
        }}
      />
      <FileVersionsDialog
        driveId={driveId}
        nodeId={versionsOf?.nodeId}
        open={versionsOf !== null}
        onClose={() => setVersionsOf(null)}
        {...(versionsOf ? { subjectName: versionsOf.subjectName } : {})}
      />
      <MoveToDialog
        open={moveOpen}
        driveId={driveId}
        startFolderId={currentFolderId}
        {...(currentFolderName ? { startLabel: currentFolderName } : {})}
        {...(currentTrail ? { startTrail: currentTrail } : {})}
        moving={selection}
        count={selection.length}
        platform={platform}
        onCancel={() => setMoveOpen(false)}
        onConfirm={(parentId, destination, movable) => {
          // Only the rows this destination will take: a folder that cannot go
          // inside itself does not hold back the files selected beside it.
          moveInto(movable ?? selection, parentId, destination);
          setMoveOpen(false);
        }}
      />
    </>
  );
}

export default FilesActions;

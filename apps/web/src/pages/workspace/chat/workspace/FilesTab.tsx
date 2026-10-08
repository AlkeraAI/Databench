/**
 * The chat's own file explorer, as a workspace tab.
 *
 * It is the SAME explorer the Files page mounts — the same browser, the same
 * actions, the same trail rule, the same drag-and-drop verdicts — rooted at the
 * one directory the chat's agent runs in. Reusing it is the point: a person who
 * has learned to rename, share or drag a file in Files does not have to learn a
 * second, smaller thing here, and a rule fixed in one place is fixed in both.
 *
 * Three things are this surface's own. The root is a ceiling, not a starting
 * point: everything under the chat's working directory is the chat's, and a
 * navigation that would leave it — the chat folder above, a place on the rail —
 * is ignored rather than followed, because a panel beside a conversation that
 * wandered off into the drive is a panel the person cannot get back. Opening a
 * file does not leave the page: it opens a tab beside this one, which is what
 * "open" means in a workspace. And the header is one line — the conversation's
 * mark, its title cut with an ellipsis, then the buttons — because this
 * explorer lives in a column the reader can drag narrow, and a header that
 * answers a narrow column by growing rows eats the listing. What the machine
 * is doing to the folder rides the status bar at the foot of the tab instead,
 * where a permanent fact does not cost the listing a row.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";

import { SidePanel, type ContextMenuItem } from "@alkera/ui";
import { useQueryClient } from "@tanstack/react-query";

import { useCurrentUser } from "@/api/auth";
import { flattenChildren, useChildren, useCreateFolder, useItem, type Item } from "@/api/files";
import { keys } from "@/api/keys";
import { Icon } from "@/app/icons";
import { userScope } from "@/lib/accountScope";
import { isChatFilesNode } from "@/lib/files/chatFolder";
import { filesErrorCopy, type FilesErrorCopy } from "@/lib/files/errors";
import { displayNameOf } from "@/lib/files/columns";
import { DEFAULT_ORDER, FilesBrowser } from "@/pages/workspace/files/FilesBrowser";
import {
  chatRecordsRule,
  useShowHiddenFiles,
  visibleRows,
} from "@/pages/workspace/files/hiddenEntries";
import { FilesActions, type FilesActionsApi } from "@/pages/workspace/files/FilesActions";
import { LiveRowChip } from "@/pages/workspace/files/live/LiveRowChip";
import { countOnBox, useFolderLiveness } from "@/pages/workspace/files/live/useFolderLiveness";
import { openTargetOf, pageDoorOf } from "@/lib/files/openTarget";
import { RenameInline } from "@/pages/workspace/files/RenameInline";
import { RightPane } from "@/pages/workspace/files/RightPane";
import { detectPlatform } from "@/lib/platform";
import { leaseFacet, relativeTime, useNow } from "@/pages/workspace/files/useLeaseFacet";
import { useCrumbTrail } from "@/pages/workspace/files/useCrumbTrail";
import { dropTargetOf, useFilesDragDrop } from "@/pages/workspace/files/useFilesDragDrop";
import { UploadTray } from "@/pages/workspace/files/UploadTray";
import { useUploads, type DropTransfer } from "@/pages/workspace/files/useUploads";

import { FilesStatusBar } from "./FilesStatusBar";
import { DEFAULT_NOTEBOOK_NAME, notebookFileName } from "./notebook/notebookNames";
import { NOTEBOOK_VIEW } from "./notebook/register";
import { dockColumnLayout, hiddenColumnsAttr } from "./dockColumns";
import { registerTabKind, type WorkspaceCtx, type WorkspaceTab } from "./tabKinds";
import { usePaneWidth } from "./usePaneWidth";
import { useLeaseNodeId } from "./useLeaseNodeId";
import { previewsBesideBrowser, useWorkspaceStore } from "./workspaceStore";

import "./workspace.css";

/** The create buttons the bar carries: the Files page's two, then a notebook,
 *  which opens beside the listing as soon as it exists. */
const CREATE_ACTIONS = [
  ["new-folder", "New folder"],
  ["new-notebook", "New notebook"],
  ["upload-files", "Upload"],
] as const;

/** What the create field is naming. */
type Naming = "folder" | "notebook";

/** The folder this tab is showing: the one it remembered, else the root. A
 *  remembered folder that has since gone is corrected by the read that fails,
 *  not guessed at here. */
function folderOf(tab: WorkspaceTab, rootNodeId: string): string {
  const stored = tab.params?.["folderId"];
  return typeof stored === "string" && stored !== "" ? stored : rootNodeId;
}

export function FilesTab({ tab, ctx }: { tab: WorkspaceTab; ctx: WorkspaceCtx }) {
  const { chatId, driveId, rootNodeId } = ctx;
  const folderId = folderOf(tab, rootNodeId);

  const setFilesFolder = useWorkspaceStore((state) => state.setFilesFolder);
  const openFileTab = useWorkspaceStore((state) => state.openFileTab);
  const clearReveal = useWorkspaceStore((state) => state.clearReveal);
  const revealId = useWorkspaceStore((state) => state.chats[chatId]?.revealId ?? null);
  // A single click opens a passing preview only where it lands beside this
  // browser; in one group a click selects and a double click opens.
  const previews = useWorkspaceStore((state) => previewsBesideBrowser(state.chats[chatId]));

  const folder = useItem(driveId, folderId);
  // The node above, read for one reason: when it is the chat that owns this
  // directory, the trail shows the CONVERSATION — its title and its mark — and
  // never the directory's own machine-minted name.
  const above = useItem(driveId, folder.data?.parentId ?? undefined);
  const dressedAsChat = isChatFilesNode(folder.data, above.data) ? above.data : undefined;
  const chain = useCrumbTrail(folder.data, dressedAsChat);

  // The same listing the browser reads, asked for with the same order, so the
  // two share one query rather than issuing two of it.
  const children = useChildren(driveId, folderId, { orderBy: DEFAULT_ORDER });
  const listed = useMemo(() => flattenChildren(children.data), [children.data]);
  // The rows the browser shows, by the same rule and the same viewer choice,
  // so what the status bar counts is what the listing draws.
  const omit = useMemo(() => chatRecordsRule(folder.data), [folder.data]);
  const [showHidden] = useShowHiddenFiles();
  const rows = useMemo(() => visibleRows(listed, omit, showHidden), [listed, omit, showHidden]);

  const openFolderIds = useMemo(
    () => (folderId === rootNodeId ? [rootNodeId] : [rootNodeId, folderId]),
    [folderId, rootNodeId],
  );

  // The machine takes the CHAT's folder, not the working directory inside it,
  // so that node is the one whose lease says whether this listing is live and
  // the one a lease frame names — the same derivation a file open in a tab
  // follows, so the two never disagree about which lease they are watching.
  const workingDir = useItem(driveId, rootNodeId);
  const leaseNodeId = useLeaseNodeId(driveId, rootNodeId);
  const live = useFolderLiveness(
    driveId,
    leaseNodeId,
    openFolderIds,
    countOnBox(rows),
    ctx.machineReady,
  );

  // The way up is the folder's own parent, never the trail: a deep link into a
  // subfolder arrives with a trail of one segment and still has a parent. The
  // working directory is the ceiling, so from it there is no way up — and the
  // node above it is the chat, which the trail names the directory after, so
  // the control says the conversation's title where the trail would.
  const parentTarget = folderId === rootNodeId ? undefined : (folder.data?.parentId ?? undefined);
  const chatNode = useItem(driveId, workingDir.data?.parentId ?? undefined);
  const rootShownAs = isChatFilesNode(workingDir.data, chatNode.data)
    ? chatNode.data
    : workingDir.data;
  const parentName =
    parentTarget === undefined
      ? undefined
      : parentTarget === rootNodeId
        ? rootShownAs && displayNameOf(rootShownAs)
        : (chain.find((segment) => segment.id === parentTarget)?.name ??
          (above.data && displayNameOf(above.data)));
  const upTitle = parentName ? `Up to ${parentName}` : "Up";

  // What the machine is doing to each row, beside the row it is doing it to.
  // The notice above says the folder is live; this is the part a reader acts on
  // — which file is mid-write, and which one is written but still on the box.
  const rowAdornment = useCallback(
    (row: Item) => (
      <>
        <LiveRowChip live={row.live} lease={row.lease} />
      </>
    ),
    [],
  );

  // The bar at the foot of the tab says the same thing in every state, so the
  // clock ticks whatever the lease is doing: a settled folder counts up from
  // when its copy was written, and a live one has nothing to count.
  const now = useNow(30_000);
  const liveness = live.liveness;
  const savedAgo = liveness.state === "persisted" ? relativeTime(liveness.asOf, now) : null;

  // The same folder in Files, one tab over. Withheld on the same asymmetry the
  // Files page withholds it for: when the folder behind this tab cannot be
  // read, the page it opens is "this isn't here", and spending the reader's
  // click on that is worse than not offering the click.
  const inFilesHref =
    rootNodeId === "" || workingDir.isError ? null : `/files/${encodeURIComponent(rootNodeId)}`;

  // Unfinished uploads are remembered under the reader and the org they are in.
  const uploader = userScope(useCurrentUser().data);
  const uploads = useUploads({ driveId: driveId ?? "", account: uploader });
  const dragging = useRef<readonly Item[]>([]);
  const [selectedIds, setSelectedIds] = useState<readonly string[]>([]);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [naming, setNaming] = useState<Naming | null>(null);
  /** The notebook being created, by its file name, while its upload is out. */
  const [creatingNotebook, setCreatingNotebook] = useState<string | null>(null);
  /** What the name field starts from: the default, or what was typed before a
   *  refusal handed the field back. */
  const [notebookDraft, setNotebookDraft] = useState(DEFAULT_NOTEBOOK_NAME);
  const queryClient = useQueryClient();
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [createRefusal, setCreateRefusal] = useState<FilesErrorCopy | null>(null);
  const createFolder = useCreateFolder();
  const filePicker = useRef<HTMLInputElement | null>(null);

  // The dock is a column the reader drags, so the listing's columns are
  // decided by THIS pane's width rather than the window's.
  const pane = useRef<HTMLDivElement | null>(null);
  const paneWidth = usePaneWidth(pane);
  const columns = useMemo(() => dockColumnLayout(paneWidth), [paneWidth]);

  const rowsById = useMemo(() => new Map(rows.map((row) => [row.id, row])), [rows]);
  const selection = useMemo(
    () => selectedIds.map((id) => rowsById.get(id)).filter((row): row is Item => row !== undefined),
    [selectedIds, rowsById],
  );

  // A folder the machine is holding is not writable from here, whatever the
  // reader's own rung says: until the server admits an inbound write, every
  // write it would take is refused at the door anyway, and offering it is worse
  // than not offering it.
  const lease = leaseFacet(folder.data);
  const heldByMachine = lease !== null && lease.inbound !== true;
  const canWriteHere = (folder.data?.capabilities?.can_write ?? false) && !heldByMachine;

  const { dropOn, dragDropFor, listingProps, listingDropActive, hereName } = useFilesDragDrop({
    driveId,
    folder: folder.data,
    folderId,
    displayAs: dressedAsChat,
    canWriteHere,
    rows,
    selection,
    uploads,
    dragging,
  });

  // The root is a ceiling. A segment of the trail and a folder on screen are
  // both inside it; the chat folder above it and a place on the rail are not,
  // and are dropped on the floor rather than followed.
  const navigate = useCallback(
    (nodeId: string) => {
      // A rename left open belongs to the folder it was started in, so walking
      // away from that folder abandons it rather than carrying the field over
      // to whichever row lands in the same place.
      setRenamingId(null);
      if (nodeId === rootNodeId) {
        setFilesFolder(chatId, rootNodeId);
        return;
      }
      const onTrail = chain.some((segment) => segment.id === nodeId);
      const listedHere = rowsById.get(nodeId)?.kind === "folder";
      // The folder above the one on screen is inside the ceiling whenever the
      // one on screen is, whether or not the trail walked through it.
      const isParent = nodeId === parentTarget;
      if (!onTrail && !listedHere && !isParent) return;
      setFilesFolder(chatId, nodeId);
    },
    [chain, chatId, parentTarget, rootNodeId, rowsById, setFilesFolder],
  );

  // The Files page's own inline editor, on the row the reader asked to rename.
  // It owns its request and its refusal, so the tab only says which row is
  // being renamed and when it has stopped.
  const renderNameOverride = useCallback(
    (row: Item) =>
      row.id === renamingId && driveId !== undefined ? (
        <RenameInline driveId={driveId} item={row} onDone={() => setRenamingId(null)} />
      ) : null,
    [renamingId, driveId],
  );

  const open = useCallback(
    (item: Item) => {
      const target = openTargetOf(item);
      if (target.kind === "page") {
        // A row that IS a page (a chat, a workspace, a template) is a whole
        // screen, so it opens away from the panel.
        window.open(target.to, "_blank", "noopener");
        return;
      }
      if (target.kind === "folder") {
        navigate(target.nodeId);
        return;
      }
      openFileTab(chatId, {
        nodeId: item.id,
        name: displayNameOf(item),
        path: item.path ?? null,
        parentId: item.parentId ?? null,
      });
    },
    [chatId, navigate, openFileTab],
  );

  const preview = useCallback(
    (item: Item) => {
      // A row that is a page or a folder opens as one, never as a passing tab.
      if (openTargetOf(item).kind !== "file") return;
      openFileTab(
        chatId,
        {
          nodeId: item.id,
          name: displayNameOf(item),
          path: item.path ?? null,
          parentId: item.parentId ?? null,
        },
        { transient: true },
      );
    },
    [chatId, openFileTab],
  );

  // A reveal is a one-shot request: once the row has been put in front of the
  // reader, the id is dropped so their next click is not undone by it.
  useEffect(() => {
    if (revealId === null) return;
    if (!rowsById.has(revealId)) return;
    const timer = setTimeout(() => clearReveal(chatId), 0);
    return () => clearTimeout(timer);
  }, [revealId, rowsById, chatId, clearReveal]);

  const active = selection.length === 1 ? selection[0] : undefined;

  // The keyboard and the captions beside the menu rows are the reader's own
  // platform's, as they are on the Files page. Left unsaid the action layer
  // assumes a PC, where Backspace alone is the delete — so a reader who clicks
  // a row here and presses Backspace on a Mac trashes the file — and it then
  // refuses every Cmd chord as the wrong platform's modifier.
  const platform = useMemo(() => detectPlatform(), []);

  return (
    <FilesActions
      driveId={driveId}
      platform={platform}
      currentFolderId={folderId}
      currentFolderName={
        dressedAsChat
          ? displayNameOf(dressedAsChat)
          : folder.data
            ? displayNameOf(folder.data)
            : undefined
      }
      selection={selection}
      canWriteHere={canWriteHere}
      // The lease, said twice because the menu asks it twice: once of the
      // folder, for what may be created in it, and once of each row, for what
      // may be changed. Every row here sits in the folder this tab is listing,
      // so they answer alike. Without it a row's `can_rename` — a write answer
      // the server gives without regard to any lease — leaves Rename offered
      // on a folder no write of the reader's can reach.
      leasedHere={heldByMachine}
      isHeld={() => heldByMachine}
      onOpen={open}
      onOpenPage={(row) => {
        // A page is a whole screen, so it opens away from the panel.
        const door = pageDoorOf(row);
        if (door !== null) window.open(door.to, "_blank", "noopener");
      }}
      onRename={(row) => setRenamingId(row.id)}
      onOpenParent={() => {
        if (parentTarget !== undefined) navigate(parentTarget);
      }}
      onNewFolder={() => setNaming("folder")}
      onNewNotebook={() => setNaming("notebook")}
      onUploadFiles={() => filePicker.current?.click()}
      onDetails={() => setDetailsOpen(true)}
    >
      {(acts: FilesActionsApi) => (
        <div
          ref={pane}
          className="alk-ws-files"
          // The listing is the Files page's five-column table, and the dock is
          // narrower than its floors: the columns it cannot draw whole are named
          // here and hidden by the rules beside them, with the tracks that are
          // left handed over as a variable so the row stays a grid.
          data-hide-columns={hiddenColumnsAttr(columns)}
          style={{ "--chat-dock-grid": columns.template } as CSSProperties}
          onKeyDown={acts.onKeyDown}
          onContextMenu={acts.triggerProps.onContextMenu}
          {...listingProps(acts)}
        >
          {listingDropActive ? (
            <div className="alk-files__drop-hint" aria-hidden="true">
              Drop to upload into {hereName}
            </div>
          ) : null}
          <input
            ref={filePicker}
            type="file"
            multiple
            hidden
            aria-label="Upload files"
            onChange={(event) => {
              void uploads.onDrop(
                dropTargetOf(folder.data),
                pickedTransfer(Array.from(event.target.files ?? [])),
              );
              event.target.value = "";
            }}
          />
          <FilesBrowser
            driveId={driveId}
            parentId={folderId}
            up={{
              title: upTitle,
              go: parentTarget === undefined ? undefined : () => navigate(parentTarget),
            }}
            chain={chain}
            orderBy={DEFAULT_ORDER}
            onNavigate={navigate}
            onOpen={open}
            {...(previews ? { onPreview: preview } : {})}
            onDropToSegment={(id, event) => dropOn(id, event, acts)}
            dragDrop={dragDropFor(acts)}
            omit={omit}
            renderNameOverride={renderNameOverride}
            rowAdornment={rowAdornment}
            onSelectionChange={setSelectedIds}
            revealId={revealId}
            actions={
              <>
                <div className="alk-ws-files__acts">
                  <TrashButton row={acts.menuItems.find((entry) => entry.id === "trash")} />
                  {inFilesHref === null ? null : (
                    <a
                      className="alk-files-browser__create alk-ws-files__popout"
                      href={inFilesHref}
                      target="_blank"
                      rel="noopener"
                      aria-label="Open in Files"
                      title="Open in Files"
                    >
                      <Icon name="external" size={14} />
                    </a>
                  )}
                  {naming !== null && driveId !== undefined ? (
                    <form
                      className="alk-files__new-folder"
                      aria-label={naming === "folder" ? "New folder" : "New notebook"}
                      onSubmit={(event) => {
                        event.preventDefault();
                        const field = new FormData(event.currentTarget).get("name");
                        const name = typeof field === "string" ? field.trim() : "";
                        if (naming === "notebook") {
                          if (creatingNotebook !== null) return;
                          // The form closes at once: the bar says what is being
                          // created until the upload lands and the notebook opens.
                          setCreateRefusal(null);
                          setNaming(null);
                          setCreatingNotebook(notebookFileName(name));
                          // Loaded on first use: the pane opens without it.
                          void import("./notebook/newNotebook")
                            .then(({ createNotebook }) => createNotebook(driveId, folderId, name))
                            .then((created) => {
                              setNotebookDraft(DEFAULT_NOTEBOOK_NAME);
                              void queryClient.invalidateQueries({
                                queryKey: keys.files.childrenOf(driveId, folderId),
                              });
                              openFileTab(
                                chatId,
                                {
                                  nodeId: created.nodeId,
                                  name: created.name,
                                  path: null,
                                  parentId: folderId,
                                },
                                { view: NOTEBOOK_VIEW },
                              );
                            })
                            .catch((error: unknown) => {
                              // Not created: the field comes back holding what was
                              // typed, beside the reason.
                              setNotebookDraft(name === "" ? DEFAULT_NOTEBOOK_NAME : name);
                              setCreateRefusal(filesErrorCopy(error, { action: "create" }));
                              setNaming("notebook");
                            })
                            .finally(() => setCreatingNotebook(null));
                          return;
                        }
                        if (name === "") {
                          setNaming(null);
                          return;
                        }
                        setCreateRefusal(null);
                        createFolder.mutate(
                          { driveId, parentId: folderId, name },
                          {
                            onSuccess: () => {
                              setCreateRefusal(null);
                              setNaming(null);
                            },
                            // The field stays open holding what was typed, beside the
                            // reason: the folder does not exist, and closing the form
                            // would say the opposite.
                            onError: (error) =>
                              setCreateRefusal(filesErrorCopy(error, { action: "create" })),
                          },
                        );
                      }}
                    >
                      <input
                        key={naming}
                        name="name"
                        aria-label={naming === "folder" ? "Folder name" : "Notebook name"}
                        defaultValue={naming === "notebook" ? notebookDraft : undefined}
                        onFocus={(event) => event.currentTarget.select()}
                        autoFocus
                        autoComplete="off"
                      />
                      <button type="submit">Create</button>
                      <button
                        type="button"
                        onClick={() => {
                          setNaming(null);
                          setCreateRefusal(null);
                        }}
                      >
                        Cancel
                      </button>
                      {createRefusal ? (
                        <p className="alk-files__refusal" role="status">
                          {createRefusal.title}
                        </p>
                      ) : null}
                    </form>
                  ) : (
                    <div className="alk-files-browser__creates" role="group" aria-label="Create">
                      {creatingNotebook !== null ? (
                        <span className="alk-ws-files__creating" role="status">
                          Creating {creatingNotebook}
                        </span>
                      ) : null}
                      {CREATE_ACTIONS.map(([action, label]) => (
                        <button
                          key={action}
                          type="button"
                          className="alk-files-browser__create"
                          disabled={
                            !canWriteHere ||
                            (action === "new-notebook" && creatingNotebook !== null)
                          }
                          onClick={() => acts.run(action)}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </>
            }
          />
          <div className="alk-files-tray">
            <UploadTray
              rows={uploads.rows}
              skippedSidecars={uploads.skippedSidecars}
              identicalCopies={uploads.identicalCopies}
              alreadyInFiles={uploads.alreadyInFiles}
              finished={uploads.finished}
              refusal={uploads.refusal}
              batch={uploads.batch}
              conflicts={uploads.conflicts}
              resumable={uploads.resumable}
              onPause={uploads.pause}
              onResume={uploads.resume}
              onCancel={uploads.cancel}
              onAnswerConflict={uploads.answerConflict}
              onRetry={(id) => void uploads.retry(id)}
              onRetryFailed={() => void uploads.retryFailed()}
              onDismiss={uploads.dismiss}
            />
          </div>
          <FilesStatusBar
            liveness={liveness}
            streamDown={live.streamDown}
            offline={live.offline}
            savedAgo={savedAgo}
          />
          <SidePanel
            open={detailsOpen && active !== undefined}
            onClose={() => setDetailsOpen(false)}
            anchor="belowTopbar"
            mode="inline"
            title="Details"
          >
            <RightPane driveId={driveId} item={active} selectedCount={selection.length} />
          </SidePanel>
        </div>
      )}
    </FilesActions>
  );
}

/**
 * Move to trash, on the bar, for a reader who never right-clicks.
 *
 * It is the menu's own trash row and nothing else: its label, its refusal and
 * the action it runs all come from the row the Files page's menu built for this
 * selection, so the bar cannot trash what the menu refuses or trash it another
 * way. With nothing selected the row does not exist and neither does the
 * button; refused, it stays disabled and says why on hover.
 */
function TrashButton({ row }: { row: ContextMenuItem | undefined }) {
  if (row === undefined) return null;
  const refused = row.disabled === undefined || row.disabled === "" ? undefined : row.disabled;
  return (
    <button
      type="button"
      className="alk-files-browser__create alk-ws-files__trash"
      aria-label={row.label}
      title={refused ?? row.label}
      disabled={refused !== undefined}
      onClick={() => row.onSelect?.()}
    >
      <Icon name="trash" size={14} />
    </button>
  );
}

/** The picked files, in the shape a drop hands the upload session. The picker
 *  here takes files and not a folder, so there is no tree to rebuild. */
function pickedTransfer(files: readonly File[]): DropTransfer {
  return {
    items: files.map((file) => ({
      kind: "file",
      webkitGetAsEntry: () => null,
      getAsFile: () => file,
    })),
    getData: () => "",
  };
}

/** The tab kind. Registered at import: the chat's workspace always has a Files
 *  tab, so the module that draws it is the module that says so. */
registerTabKind({
  kind: "files",
  pinned: true,
  singleton: true,
  label: () => "Files",
  Component: FilesTab,
});

export default FilesTab;

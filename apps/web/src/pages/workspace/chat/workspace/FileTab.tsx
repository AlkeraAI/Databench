/**
 * One file, open beside the conversation, shown one of the ways it can be.
 *
 * The tab is a thin frame around three things the product already owns (the
 * viewer registry that says which ways a type can be shown, `fileViewers`; the
 * preview registry that decides how each is drawn; and the grant-and-fetch hook
 * that buys its bytes), so a file looks and behaves the same here, in the Files
 * page's large preview, and on the page a share link lands on.
 *
 * What belongs to this surface is everything the registries deliberately do not
 * know about:
 *
 *  * **Only a tab in front of its group holds bytes.** A workspace can hold
 *    dozens of tabs, each of which would otherwise keep a frame, a blob URL and
 *    a live grant alive for a file nobody is looking at. So the item is withheld
 *    from the bytes hook unless this tab is the one its group shows, and a tab
 *    in the background costs one item read and nothing else.
 *  * **A tab in the background still watches its file.** Withholding the bytes
 *    is not the same as looking away: the node is read and re-read on the
 *    frames that name it, so a file the machine rewrites while the reader is
 *    elsewhere comes back marked rather than silently stale.
 *  * **A change is announced once, not once per write.** An agent writing a
 *    report rewrites it many times a minute; a live region that repeated every
 *    one of them would make the page unusable with a screen reader on. One
 *    announcement per file per five seconds is the ceiling.
 *  * **A file can stop existing while it is open.** A tab that keeps drawing
 *    the last copy it saw is a lie the reader acts on, so a node that has gone
 *    says so, and offers the only two things left to do with it.
 *  * **A live file has no unsaved state.** What is typed is the file; a preview
 *    of it follows the document as it is typed (Markdown, a table) or as each
 *    edit is written back (a page or a drawing, which only ever render in the
 *    sandboxed frame on the content origin).
 */

import {
  IconCopy,
  IconDownload,
  IconExternalLink,
  IconFolderSearch,
  IconLayoutSidebarRight,
  IconLink,
  IconShare,
  IconTextWrap,
} from "@tabler/icons-react";
import { useQueryClient } from "@tanstack/react-query";
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  Button,
  EmptyState,
  PreviewSurface,
  SegmentedControl,
  planPreviewWith,
  type PreviewContent,
} from "@alkera/ui";

import { useUserScope } from "@/api/auth";
import { LIVE_DOC_SAVED, isHeldLive } from "@/api/events/machineRefresh";
import { EDITS_NOT_KEPT, type LiveNotice } from "@/api/realtime/crdt/channel";
import { useFrames } from "@/api/events/frameBus";
import {
  useItem,
  useMintContentGrant,
  useRestoreTrash,
  useTrash,
  type Item,
  type MintContentGrant,
} from "@/api/files";
import { keys } from "@/api/keys";

import {
  displayNameOf,
  formatModified,
  formatSize,
  modifiedOf,
  sizeOf,
} from "@/lib/files/columns";
import { contentUrl, downloadItem } from "@/lib/files/download";
import { filesErrorCopy } from "@/lib/files/errors";
import { copyLinkTo } from "@/lib/files/links";
import { LiveRowChip } from "@/pages/workspace/files/live/LiveRowChip";
import { useLiveNode } from "@/pages/workspace/files/live/useLiveNode";
import { openExternal } from "@/pages/workspace/files/preview/openExternal";
import { copyPreviewContent, copyableKind } from "@/pages/workspace/files/preview/copyContent";
import {
  previewFacts,
  usePreviewContent,
  type PreviewBytes,
} from "@/pages/workspace/files/preview/usePreviewContent";
import { ShareDialog } from "@/pages/workspace/files/ShareDialog";

import { FileTabHeader, type FileTabAction } from "./FileTabHeader";
import { LiveEditsNotice } from "./LiveEditsNotice";
import { useSteadyPreview } from "./FileTabPreview";
import { wrapsByDefault } from "./fileLanguages";
import {
  counterpart,
  fileViewerFor,
  offersEditing,
  previewOf,
  readOnlyRendererFor,
  resolveView,
} from "./fileViewers";
import "./notebook/register";
import { registerTabKind, type WorkspaceCtx, type WorkspaceTab } from "./tabKinds";
import { useLeaseNodeId } from "./useLeaseNodeId";
import { useLiveText } from "./useLiveText";
import { isVisible, useWorkspaceStore } from "./workspaceStore";

import "./workspace.css";

/** Whether a file edited live wraps: as the reader set it, else by its kind. */
function liveWrap(viewState: Record<string, unknown> | undefined, name: string): boolean {
  const set = viewState?.["wrap"];
  return typeof set === "boolean" ? set : wrapsByDefault(name);
}

/** The live editor, loaded with the first text file a reader opens. */
const LiveFileEditor = lazy(() => import("./LiveFileEditor"));

/** Whether a file tab offers `item` to be edited live: a file the drive still
 *  holds, of a type with an editing view. Whether it is small enough, and who
 *  may type, the server decides when the editor asks for it. */
export function offersLiveEditing(item: Item | undefined): boolean {
  if (item === undefined || item.kind !== "file" || item.trashed === true) return false;
  return offersEditing(fileViewerFor(previewFacts(item)));
}

/** What the tab says once its file is not in the chat any more. */
export const GONE_TITLE = "This file is no longer in the chat";

/** What a file's text says when the editor cannot open it and it is shown
 *  read-only instead. */
export const READ_ONLY = "View only";

/** The floor between two announcements of the same tab's file. Below it the
 *  change is still marked on the tab; it is only the speech that is dropped. */
export const ANNOUNCE_EVERY_MS = 5_000;

const COPIED = "Link copied";
const COPY_FAILED = "Couldn't copy the link";
const CONTENT_COPIED = "Copied";
const CONTENT_COPY_FAILED = "Couldn't copy";

/** The file's path as the reader knows it: relative to the chat's own working
 *  directory, so a tab says `charts/q3.png` and not the whole drive chain.
 *
 *  The root's path is what makes it relative, and the root is read from the
 *  drive rather than assumed, so a chat whose directory has been moved still
 *  names its files correctly. With no root path to strip, the file's own name is
 *  the honest answer — better a short truth than a long one about the drive. */
export function chatRelativePath(
  pathBytes: string | null | undefined,
  rootPathBytes: string | null | undefined,
  name: string,
): string {
  if (typeof pathBytes !== "string" || pathBytes === "") return name;
  if (typeof rootPathBytes !== "string" || rootPathBytes === "") return name;
  const root = rootPathBytes.endsWith("/") ? rootPathBytes : `${rootPathBytes}/`;
  if (!pathBytes.startsWith(root)) return name;
  const relative = pathBytes.slice(root.length);
  return relative === "" ? name : relative;
}

/** The trash operation that removed this node, when the drive still lists it as
 *  a trashed root. A file trashed inside a folder is not a root of its own — the
 *  folder is — so there is nothing here to restore on its own, and the tab says
 *  so by offering no Restore rather than by failing one. */
function trashOpFor(
  pages: readonly { entries?: readonly { trashOpId: string; item: Item }[] | null }[] | undefined,
  nodeId: string | undefined,
): string | null {
  if (!nodeId) return null;
  for (const page of pages ?? []) {
    for (const entry of page.entries ?? []) {
      if (entry.item.id === nodeId) return entry.trashOpId;
    }
  }
  return null;
}

export function FileTab({ tab, ctx }: { tab: WorkspaceTab; ctx: WorkspaceCtx }) {
  const { chatId, driveId, rootNodeId } = ctx;
  const nodeId = typeof tab.node_id === "string" && tab.node_id !== "" ? tab.node_id : undefined;
  const queryClient = useQueryClient();

  const active = useWorkspaceStore((state) => isVisible(state.chats[chatId], tab.id));
  const markUpdated = useWorkspaceStore((state) => state.markUpdated);
  const markGone = useWorkspaceStore((state) => state.markGone);
  const markPresent = useWorkspaceStore((state) => state.markPresent);
  const closeTab = useWorkspaceStore((state) => state.closeTab);
  const reveal = useWorkspaceStore((state) => state.reveal);
  const pinTab = useWorkspaceStore((state) => state.pinTab);
  const setTabView = useWorkspaceStore((state) => state.setTabView);
  const openToSide = useWorkspaceStore((state) => state.openToSide);

  // The lease over this file is on the chat's folder, and a frame for that
  // lease is the plane's word about this file — writing, uploading, left on
  // the machine, settled — so the node is re-read on it, not only on its own
  // save. That is what keeps an open file's chip and bytes moving with the
  // agent rather than one save behind it.
  const leaseNodeId = useLeaseNodeId(driveId, rootNodeId);
  const node = useLiveNode(driveId, nodeId, leaseNodeId);
  const item = node.item;
  // The chat's working directory, for the one thing this tab needs from it: the
  // path a reader recognises. It is the same cache entry the file browser beside
  // this tab reads, so it costs no extra request.
  const root = useItem(driveId, rootNodeId);

  const name = item ? displayNameOf(item) : tab.name;
  const relativePath = chatRelativePath(item?.pathBytes, root.data?.pathBytes, name);

  // Which ways this file can be shown, and the one this tab shows.
  const facts = useMemo(() => previewFacts(item), [item]);
  const viewer = useMemo(() => fileViewerFor(facts), [facts]);
  const view = resolveView(viewer, tab.view);
  const sidePreview = previewOf(viewer);

  // `mutateAsync` is a new function on every render of the mutation hook, and the
  // bytes hook keys its fetch on the mint it was handed — an unstable one would
  // buy the file again forever.
  const mutation = useMintContentGrant();
  const latest = useRef(mutation.mutateAsync);
  latest.current = mutation.mutateAsync;
  const mint = useCallback<MintContentGrant>((vars) => latest.current(vars), []);

  // A file is held live unless the server would not open it that way
  // (remembered per node, so a refused file is not asked again on every render).
  const [liveRefused, setLiveRefused] = useState<Record<string, string>>({});
  const liveSocket = ctx.liveSocket;
  const account = useUserScope();
  // Text the live document could not keep, per node, until the reader
  // dismisses it: held here, above the editor, so it stays on screen if the
  // editor goes (the file stopped being editable live).
  const [offers, setOffers] = useState<Record<string, readonly LiveNotice[]>>({});
  const offerBack = useCallback(
    (notice: LiveNotice) => {
      if (nodeId === undefined) return;
      setOffers((current) => {
        const shown = current[nodeId] ?? [];
        // Two holders of one file (its editor and its rendering) hand over the same offer.
        if (shown.some((n) => n.message === notice.message && n.restorable === notice.restorable)) return current;
        return { ...current, [nodeId]: [...shown, notice] };
      });
    },
    [nodeId],
  );
  const dismissOffer = useCallback(
    (notice: LiveNotice) => {
      if (nodeId !== undefined) setOffers((current) => ({ ...current, [nodeId]: (current[nodeId] ?? []).filter((n) => n !== notice) }));
    },
    [nodeId],
  );
  // Never before the reader is known: what a live document keeps for the next
  // page is filed under them, and one opened for nobody would keep nothing.
  const liveable =
    liveSocket !== undefined &&
    account !== null &&
    nodeId !== undefined &&
    liveRefused[nodeId] === undefined &&
    item !== undefined &&
    item.kind === "file" &&
    item.trashed !== true &&
    offersEditing(viewer);
  const editing = liveable && view.draw.kind === "live-editor";
  const refuseLive = useCallback(
    (reason: string) => {
      if (nodeId !== undefined) setLiveRefused((current) => ({ ...current, [nodeId]: reason }));
    },
    [nodeId],
  );

  // A rendering drawn from the live document's text follows it as it is typed.
  const drawsLiveText = liveable && view.draw.kind === "render" && view.draw.live === true;
  const liveText = useLiveText(nodeId, liveSocket, { enabled: active && drawsLiveText && !node.gone, account });
  useEffect(() => {
    if (liveText.kind !== "fallback") return;
    if (liveText.unacknowledged) offerBack({ message: EDITS_NOT_KEPT, restorable: liveText.unacknowledged });
    refuseLive(liveText.reason);
  }, [liveText, offerBack, refuseLive]);

  // The renderer this view draws with: the one it names, or, for an editor
  // that cannot open the file, the text read-only.
  const rendererId =
    view.draw.kind === "render"
      ? view.draw.renderer
      : view.draw.kind === "live-editor"
        ? readOnlyRendererFor(facts)
        : undefined;

  // The whole "only the tab in front holds bytes" rule, in one expression: a tab
  // in the background hands the hook nothing, so it mints no grant, fetches no
  // bytes and holds no blob URL. A file being edited live, or drawn from its live
  // text, holds its text in the document and buys no bytes either.
  const wantsBytes = active && !node.gone && !editing && !drawsLiveText && view.draw.kind !== "custom";
  const fetched = usePreviewContent(wantsBytes ? item : undefined, mint, rendererId);
  // A rewrite is not a disappearance: the copy on screen stays while the next
  // one is bought, so the pane never blinks through an empty frame and the
  // reader keeps their place in the file. Kept per view, so switching from the
  // drawing to the source never paints the one with the other's bytes.
  const steady = useSteadyPreview(fetched, nodeId === undefined ? undefined : `${nodeId}#${view.id}`);
  const bytes: PreviewBytes = useMemo(() => {
    if (!drawsLiveText) return steady;
    const plan = planPreviewWith({ ...facts, synced: true }, rendererId);
    if (liveText.kind !== "live") {
      return { facts, version: "", plan, content: { kind: "none" }, status: "loading" };
    }
    const content: PreviewContent = { kind: "text", text: liveText.text };
    return { facts: { ...facts, synced: true }, version: `live-${liveText.revision}`, plan, content, status: "ready" };
  }, [drawsLiveText, facts, liveText, rendererId, steady]);

  // A page or a drawing is redrawn from the drive's copy, and while this tab
  // holds the file live its write backs are not re-read on their own (the
  // editor shows them). The rendering needs them, so a visible one asks.
  const redrawsOnSave = active && liveable && view.draw.kind === "render" && view.draw.live !== true;
  useFrames(
    (frame) =>
      redrawsOnSave &&
      frame.type === "file_node.changed" &&
      frame.entity_id === nodeId &&
      frame.reason === LIVE_DOC_SAVED,
    () => {
      if (nodeId !== undefined && isHeldLive(nodeId)) {
        queryClient.refetchQueries({ queryKey: keys.files.item(nodeId), type: "active" }).catch(() => undefined);
      }
    },
  );

  // The renderer's own settings live in this header, not in a bar of the
  // renderer's own, so they are held here — per node, so a wrap turned on for a
  // log is not inherited by the next file opened in its place.
  const [viewStates, setViewStates] = useState<Record<string, Record<string, unknown>>>({});
  const viewState = nodeId === undefined ? undefined : viewStates[nodeId];
  const setViewState = useCallback(
    (next: unknown) => {
      if (nodeId === undefined) return;
      setViewStates((current) => ({
        ...current,
        [nodeId]: { ...current[nodeId], ...(next as Record<string, unknown>) },
      }));
    },
    [nodeId],
  );

  const [refusal, setRefusal] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);
  const [announcement, setAnnouncement] = useState<{ text: string; seq: number } | null>(null);

  const trashed = item?.trashed === true;
  const restorable = trashed && item?.capabilities?.can_write === true;
  // Read only when there is something to restore: the trash listing is a whole
  // drive's worth of rows, and a tab on a healthy file has no use for it.
  const trash = useTrash(restorable ? driveId : undefined);
  const trashOpId = trashOpFor(trash.data, nodeId);
  const restore = useRestoreTrash();

  // A change to the BYTES is two different facts: the strip has to mark the tab
  // so a reader who is elsewhere can see it happened, and anyone listening has
  // to hear it — but only as often as a person can use. The content tag is
  // what moves when the bytes do; the etag also moves for a share, a rename, a
  // move or a trash, none of which is news about what the file says, and a dot
  // for those read as an edit that never happened.
  // A server that sends no content tag leaves only the etag to go by, which
  // is the older behaviour: every change marks.
  const ctag = node.ctag || node.etag || null;
  const seen = useRef<string | null>(null);
  const announcedAt = useRef<number>(Number.NEGATIVE_INFINITY);
  const counter = useRef(0);
  useEffect(() => {
    if (ctag === null || nodeId === undefined) return;
    const previous = seen.current;
    seen.current = ctag;
    if (previous === null || previous === ctag) return;
    // A file held live is written back as people type: the document already
    // shows every change, and marking each write back would mark the tab
    // while its reader is typing in it.
    if (editing || drawsLiveText || isHeldLive(nodeId)) return;
    markUpdated(chatId, nodeId);
    const now = Date.now();
    if (now - announcedAt.current < ANNOUNCE_EVERY_MS) return;
    announcedAt.current = now;
    counter.current += 1;
    setAnnouncement({ text: `${name} updated`, seq: counter.current });
  }, [chatId, ctag, drawsLiveText, editing, markUpdated, name, nodeId]);

  // Both halves of the same fact, so the strip stops saying "no longer
  // available" the moment the file is put back.
  useEffect(() => {
    if (nodeId === undefined) return;
    if (node.gone) markGone(chatId, nodeId);
    else if (item) markPresent(chatId, nodeId);
  }, [chatId, item, markGone, markPresent, node.gone, nodeId]);

  const download = useCallback(() => {
    if (item && driveId) downloadItem(contentUrl(driveId, item.id), item);
  }, [driveId, item]);

  const openInNewTab = useCallback(() => {
    if (!item) return;
    setRefusal(null);
    void openExternal(item, mint).catch((error: unknown) =>
      setRefusal(filesErrorCopy(error, { action: "open" }).title),
    );
  }, [item, mint]);

  // A copied link names the org it was copied in.
  const copyLink = useCallback(async () => {
    if (!item) return;
    setCopied((await copyLinkTo(item)) === "copied" ? COPIED : COPY_FAILED);
  }, [item]);
  // The bytes as the reader would paste them; offered only when they have such
  // a form (text, or an image), never for a document only a frame can draw.
  const canCopy = !editing && bytes.status === "ready" && copyableKind(bytes.content) !== null;
  const copyContent = useCallback(async () => {
    setCopied((await copyPreviewContent(bytes.content)) === "copied" ? CONTENT_COPIED : CONTENT_COPY_FAILED);
  }, [bytes.content]);

  // "Reveal" means the reader wants the file browser, not this tab: the browser
  // is pointed at the file's folder and told which row to select, and then it is
  // the tab in front.
  const revealInFiles = useCallback(() => {
    if (!nodeId) return;
    reveal(chatId, { nodeId, name, parentId: item?.parentId ?? null }, { show: "browser" });
  }, [chatId, item?.parentId, name, nodeId, reveal]);

  const previewToSide = useCallback(() => {
    if (!nodeId || !sidePreview) return;
    openToSide(
      chatId,
      { nodeId, name, path: tab.path ?? null, parentId: item?.parentId ?? null },
      { view: sidePreview.id, fromGroup: tab.group },
    );
  }, [chatId, item?.parentId, name, nodeId, openToSide, sidePreview, tab.group, tab.path]);

  const doRestore = useCallback(() => {
    if (!driveId || !item || !trashOpId) return;
    setRefusal(null);
    restore.mutate(
      { driveId, opId: trashOpId, etag: item.etag },
      { onError: (error) => setRefusal(filesErrorCopy(error, { action: "restore" }).title) },
    );
  }, [driveId, item, restore, trashOpId]);

  const closeThis = useCallback(() => closeTab(chatId, tab.id), [chatId, closeTab, tab.id]);
  const keepTab = useCallback(() => {
    if (tab.transient) pinTab(chatId, tab.id);
  }, [chatId, pinTab, tab.id, tab.transient]);

  // While the file is edited live, or drawn from its live text, its size is
  // the document's: the drive's item is a write back behind, and is not re-read
  // for this tab's own.
  const [liveBytes, setLiveBytes] = useState<number | null>(null);
  const liveTextBytes = useMemo(
    () => (drawsLiveText && liveText.kind === "live" ? new TextEncoder().encode(liveText.text).length : null),
    [drawsLiveText, liveText],
  );
  const shownFacts = useMemo(() => {
    if (!item) return [];
    const size = (editing ? liveBytes : liveTextBytes) ?? sizeOf(item);
    return [formatSize(size), `Updated ${formatModified(modifiedOf(item))}`];
  }, [editing, item, liveBytes, liveTextBytes]);

  // The doors out of the file, then whatever the view chosen for it offers.
  // The order is the order they fold in: the last one is the first to move into
  // the menu, so what a narrow pane keeps on the row is what a reader watching a
  // file being written reaches for.
  const actions = useMemo<readonly FileTabAction[]>(() => {
    const doors: FileTabAction[] = [
      {
        id: "download",
        label: "Download",
        icon: <IconDownload size={15} stroke={1.8} aria-hidden />,
        run: download,
      },
      {
        id: "open",
        label: "Open in new tab",
        icon: <IconExternalLink size={15} stroke={1.8} aria-hidden />,
        run: openInNewTab,
      },
      {
        id: "share",
        label: "Share",
        icon: <IconShare size={15} stroke={1.8} aria-hidden />,
        run: () => setSharing(true),
      },
      {
        id: "reveal",
        label: "Reveal in Files",
        icon: <IconFolderSearch size={15} stroke={1.8} aria-hidden />,
        run: revealInFiles,
      },
      {
        id: "copy-link",
        label: "Copy link",
        icon: <IconLink size={15} stroke={1.8} aria-hidden />,
        run: () => void copyLink(),
      },
    ];
    if (canCopy) {
      doors.splice(doors.findIndex((door) => door.id === "copy-link"), 0, {
        id: "copy",
        label: "Copy",
        icon: <IconCopy size={15} stroke={1.8} aria-hidden />,
        run: () => void copyContent(),
      });
    }
    // A file with a rendering beside its source offers it to the side, from the
    // source; the rendering itself is one toggle away in place.
    const side: FileTabAction[] =
      sidePreview && view.role === "edit"
        ? [
            {
              id: "preview-side",
              label: "Open preview to the side",
              icon: <IconLayoutSidebarRight size={15} stroke={1.8} aria-hidden />,
              run: previewToSide,
            },
          ]
        : [];
    // A file edited live is drawn by the editor, not the preview's renderer,
    // and soft wrap is the one setting it offers.
    if (editing) {
      const wrapped = liveWrap(viewState, name);
      return [
        ...side,
        {
          id: "view-wrap",
          label: "Soft wrap",
          icon: <IconTextWrap size={15} stroke={1.8} aria-hidden />,
          pressed: wrapped,
          run: () => setViewState({ wrap: !wrapped }),
        },
        ...doors,
      ];
    }
    const settings = (bytes.plan.renderer.viewSettings ?? []).map<FileTabAction>((setting) => {
      const on = setting.on ?? { [setting.key]: true };
      const off = setting.off ?? { [setting.key]: false };
      const pressed = viewState?.[setting.key] === on[setting.key];
      return {
        id: `view-${setting.key}`,
        label: setting.label,
        icon: setting.icon,
        pressed,
        run: () => setViewState(pressed ? off : on),
      };
    });
    return [...side, ...settings, ...doors];
  }, [
    bytes.plan.renderer.viewSettings,
    canCopy,
    copyContent,
    copyLink,
    download,
    editing,
    name,
    openInNewTab,
    previewToSide,
    revealInFiles,
    setViewState,
    sidePreview,
    view.role,
    viewState,
  ]);

  // A tab nobody is looking at draws nothing. It is still mounted, and its hooks
  // above are still watching the node — which is what lets the strip mark it.
  if (!active) return null;

  const offered = (nodeId === undefined ? undefined : offers[nodeId]) ?? [];
  const offerRows = offered.map((notice, i) => (
    <LiveEditsNotice
      key={i}
      message={notice.message}
      {...(notice.restorable ? { restorable: notice.restorable } : {})}
      onDismiss={() => dismissOffer(notice)}
    />
  ));

  if (nodeId === undefined || node.gone) {
    return (
      <div className="alk-ws-file alk-ws-file--gone">
        {offerRows}
        <EmptyState
          size="md"
          title={GONE_TITLE}
          body={refusal ?? undefined}
          action={
            <>
              {restorable && trashOpId ? (
                <Button size="sm" onClick={doRestore} disabled={restore.isPending}>
                  Restore
                </Button>
              ) : null}
              <Button size="sm" variant="secondary" fill="outline" onClick={closeThis}>
                Close
              </Button>
            </>
          }
        />
      </div>
    );
  }

  const toggle =
    item !== undefined && viewer.views.length > 1 ? (
      <SegmentedControl
        label={`Show ${name} as`}
        semantics="radio"
        size="sm"
        options={viewer.views.map((option) => ({ key: option.id, label: option.label }))}
        value={view.id}
        onChange={(next) => setTabView(chatId, tab.id, next)}
      />
    ) : null;

  const Custom = view.draw.kind === "custom" ? view.draw.Component : null;
  // An editor that cannot open the file shows its text read-only, and says so.
  const readOnly = view.draw.kind === "live-editor" && !editing && item !== undefined;

  return (
    <div className="alk-ws-file" data-view={view.id}>
      <FileTabHeader
        name={name}
        path={relativePath}
        facts={shownFacts}
        // What the machine is doing to this file right now. The preview below
        // is the drive's copy, so a file being written — or written and still
        // sitting on the box — is one the reader is seeing a moment late, and
        // saying so is the difference between a stale panel and an honest one.
        chip={<LiveRowChip live={node.live} lease={item?.lease} />}
        views={toggle}
        actions={actions}
      />
      {refusal ? (
        <p className="alk-ws-file__refusal" role="alert">
          {refusal}
        </p>
      ) : null}
      {offerRows}
      {readOnly && liveSocket !== undefined ? (
        <p className="alk-ws-file__mode">
          <span className="alk-ws-file__badge">{READ_ONLY}</span>
        </p>
      ) : null}
      <div className="alk-ws-file__body">
        {/* No key on the version: a new version is new bytes in the SAME
            viewer, not a new viewer. Keying it here would throw the element
            away on every rewrite and take the reader's scroll position with
            it. */}
        {Custom && item ? (
          <Custom tab={tab} ctx={ctx} item={item} onEdit={keepTab} />
        ) : editing && account !== null ? (
          <Suspense fallback={null}>
            <LiveFileEditor
              nodeId={nodeId}
              name={item?.name ?? name}
              onFallback={refuseLive}
              onNotice={offerBack}
              account={account}
              socket={liveSocket}
              wrap={liveWrap(viewState, item?.name ?? name)}
              onBytes={setLiveBytes}
              onLocalEdit={keepTab}
            />
          </Suspense>
        ) : (
          <PreviewSurface
            {...bytes}
            rendererId={rendererId}
            viewControls="host"
            viewState={viewState}
            onViewState={setViewState}
            onDownload={download}
            onOpenExternal={openInNewTab}
          />
        )}
      </div>
      <p className="alk-ws-file__copied" aria-live="polite">
        {copied}
      </p>
      {/* The same sentence twice in a row is not re-read by a screen reader, and
          a file rewritten by an agent produces exactly that. Keying the text on
          the announcement replaces the node, which is what makes the repeat
          land. */}
      <p className="alk-ws-file__live" role="status" aria-live="polite">
        {announcement ? <span key={announcement.seq}>{announcement.text}</span> : null}
      </p>
      <ShareDialog
        driveId={driveId}
        nodeId={nodeId}
        open={sharing}
        onClose={() => setSharing(false)}
        subjectName={name}
      />
    </div>
  );
}

/** The tab kind. Registered at import, so the workspace learns about files by
 *  loading the module that draws them rather than by a list someone maintains. */
registerTabKind({
  kind: "file",
  // A tab is named by its file alone, whichever view it shows: the view toggle
  // in the tab's own header says which one, and "Preview " ahead of the name
  // only pushed the name out of the strip.
  label: (tab, item) => (item ? displayNameOf(item) : tab.name),
  Component: FileTab,
});

/** The other view of the tab in front of a group, for the toggle's shortcut:
 *  `null` when its file offers only one. */
export function toggledView(item: Item | undefined, view: string | undefined): string | null {
  if (item === undefined) return null;
  const viewer = fileViewerFor(previewFacts(item));
  return counterpart(viewer, resolveView(viewer, view))?.id ?? null;
}

export default FileTab;

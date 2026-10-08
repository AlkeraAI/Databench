// The large preview a file opens into.
//
// It draws through the same registry a tab beside a chat draws through, and buys
// its bytes with the same hook, so the two surfaces can never disagree about what
// a type is or how it is fetched. What belongs to this component is everything
// around the bytes: the facts a reader needs to judge what they are looking at,
// the keys that act on the file, and — above all — the two cases where the right
// answer is to fetch NOTHING.
//
// Those two are the reason the item is withheld from the hook rather than the
// card being drawn over a load: a file whose downloads are off and a file in the
// trash must issue no request at all. A sentence with a request behind it is not
// a refusal — it is a refusal the server has to make again, for a reader who was
// already told no.
//
// The grant that buys the bytes never appears here. It is a bearer credential for
// as long as it lives, so it reaches an `<img>`/`<iframe>` source through React
// and nothing else: no link, no copyable field, no navigation.

import { Button, Modal, PreviewSurface, type ChatFilesResolver } from "@alkera/ui";
import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";

import { useMintContentGrant, type Item, type MintContentGrant } from "@/api/files";

import { displayNameOf, formatModified, formatSize, kindLabel, modifiedOf, sizeOf } from "@/lib/files/columns";
import { copyLinkTo } from "@/lib/files/links";
import { liveState } from "../liveRoot/liveness";
import { livenessLabel } from "../liveRoot/liveCopy";
import { openExternal } from "./openExternal";
import { usePreviewContent } from "./usePreviewContent";
import { copyPreviewContent, copyableKind } from "./copyContent";
import { previewActionsFor, type PreviewActionId } from "./previewActions";

/** Downloads are off for this node — the reader may see that it exists and no
 *  more, so there is nothing to draw and nothing to ask for. */
const SEALED = "Preview isn't available: downloads are off for this item.";
const TRASHED = "This is in the trash.";
const COPIED = "Link copied";
const COPY_FAILED = "Couldn't copy the link";
const CONTENT_COPIED = "Copied";
const CONTENT_COPY_FAILED = "Couldn't copy";

export interface FilePreviewModalProps {
  driveId: string;
  /** The row to preview. Absent renders nothing — the host may hold it through a
   *  close so the dialog can animate out. */
  item: Item | undefined;
  open: boolean;
  onClose: () => void;
  /** Present only where the file's own folder is readable — the landing page for a
   *  file shared alone has nowhere to send the reader. */
  onOpenInFiles?: () => void;
  onShare?: () => void;
  onPrev?: () => void;
  onNext?: () => void;
  /** Resolves paths relative to the file's folder, so a rendered document reaches
   *  the images stored beside it. */
  resolver?: ChatFilesResolver;
  /** Overridable so a test can watch the download without a jsdom navigation. */
  onDownload?: (driveId: string, item: Item) => void;
}

/** Save the bytes rather than navigate to them. A hidden same-origin anchor
 *  carrying `download` is the shape browsers turn into a save, and the click is
 *  the person's own so no popup blocker eats it. */
function saveToDisk(driveId: string, item: Item): void {
  if (!driveId) return;
  const url = `/api/v1/files/drives/${encodeURIComponent(driveId)}/items/${encodeURIComponent(item.id)}/content?download=1&disposition=attachment`;
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = item.nameDisplay || item.name;
  anchor.rel = "noopener";
  anchor.hidden = true;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

export function FilePreviewModal(props: FilePreviewModalProps): ReactElement | null {
  const { driveId, item, open, onClose, onOpenInFiles, onShare, onPrev, onNext, resolver } = props;

  // `mutateAsync` is re-made by every render of the mutation hook; the preview
  // hook keys its fetch on the mint it was handed, so an unstable one would
  // re-buy the bytes forever.
  const mutation = useMintContentGrant();
  const latest = useRef(mutation.mutateAsync);
  latest.current = mutation.mutateAsync;
  const mint = useCallback<MintContentGrant>((vars) => latest.current(vars), []);

  const [copied, setCopied] = useState<string | null>(null);

  const trashed = item?.trashed === true;
  const sealed = item?.capabilities?.can_download === false;
  const withheld = trashed || sealed;
  const bytes = usePreviewContent(withheld ? undefined : item, mint);

  const download = useCallback(() => {
    if (item) (props.onDownload ?? saveToDisk)(driveId, item);
  }, [driveId, item, props.onDownload]);

  const openInNewTab = useCallback(() => {
    if (item) void openExternal(item, mint);
  }, [item, mint]);

  const copyContent = useCallback(async () => {
    setCopied((await copyPreviewContent(bytes.content)) === "copied" ? CONTENT_COPIED : CONTENT_COPY_FAILED);
  }, [bytes.content]);
  // A copied link names the org it was copied in.
  const copyLink = useCallback(async () => {
    if (!item) return;
    setCopied((await copyLinkTo(item)) === "copied" ? COPIED : COPY_FAILED);
  }, [item]);

  // Stepping is on the document rather than the surface: the focus trap moves
  // focus between the action row and the preview, and a reader pressing an arrow
  // means the same thing wherever it landed. A field is the one place an arrow
  // belongs to what is under it.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName ?? "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target?.isContentEditable) return;
      if (event.key === "ArrowRight" && onNext) {
        event.preventDefault();
        onNext();
      } else if (event.key === "ArrowLeft" && onPrev) {
        event.preventDefault();
        onPrev();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onNext, onPrev]);

  // A new row is a new answer about the clipboard.
  useEffect(() => setCopied(null), [item?.id]);

  if (!item) return null;

  // Whether anything DRAWS the file decides what goes in the pane, never what
  // the action row offers: a zip nothing can render is still bytes a person can
  // take away or open whole, and an action row that thinned out with the kind
  // made the same file look differently capable depending on its extension.
  const reachable = !withheld;

  // The row is the shared table, rendered. What belongs to this surface is the
  // handler behind each id and how a button looks — never which actions exist,
  // because that is the thing three surfaces had each decided for themselves.
  const run: Record<PreviewActionId, () => void> = {
    download,
    "open-in-files": () => onOpenInFiles?.(),
    share: () => onShare?.(),
    copy: () => void copyContent(),
    "copy-link": () => void copyLink(),
    "open-new-tab": openInNewTab,
    "soft-wrap": () => undefined,
    close: onClose,
  };
  const footer = (
    <>
      {previewActionsFor({
        sealed,
        reachable,
        canOpenInFiles: onOpenInFiles !== undefined,
        canShare: onShare !== undefined,
        canCopy: reachable && bytes.status === "ready" && copyableKind(bytes.content) !== null,
      }).map((action) => (
        <Button
          key={action.id}
          {...(action.id === "close"
            ? {}
            : { variant: "secondary" as const, fill: action.id === "download" ? "outline" : "ghost" })}
          onClick={run[action.id]}
        >
          {action.label}
        </Button>
      ))}
    </>
  );

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={displayNameOf(item)}
      size="full"
      className="files-preview"
      footer={footer}
    >
      <div className="files-preview__meta">
        <span className="files-preview__kind">{kindLabel(item)}</span>
        <span className="files-preview__fact">{formatSize(sizeOf(item))}</span>
        <span className="files-preview__fact">{formatModified(modifiedOf(item))}</span>
        <span className="files-preview__live">{livenessLabel(liveState(item, Date.now()))}</span>
      </div>
      <div className="files-preview__body">
        {trashed ? (
          <p className="files-preview__notice">{TRASHED}</p>
        ) : sealed ? (
          <p className="files-preview__notice">{SEALED}</p>
        ) : (
          <PreviewSurface
            {...bytes}
            onDownload={download}
            onOpenExternal={reachable ? openInNewTab : undefined}
            resolver={resolver}
          />
        )}
      </div>
      <p className="files-preview__copied" aria-live="polite">
        {copied}
      </p>
    </Modal>
  );
}

import { useCallback, useRef, useState, type ReactNode } from "react";
import { IconDownload, IconExternalLink } from "@tabler/icons-react";

import type { BlobPage, BlobReference } from "@alkera/chat-model";

import { Button, Popover, type PopoverTriggerProps } from "../../primitives";
import { useReferenceActions, type ReferenceActionsState } from "../ReferenceStoreProvider";
import { resolveReferenceRenderer, type ReferenceRenderer } from "../registry";
import "./references.css";

// Small preview page size — the hover peek only needs the first rows/chars.
const PREVIEW_PAGE = 50;

type PreviewState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; page: BlobPage }
  | { status: "error"; message: string };

/** The host side-effects a reference chip uses, read from the reference store.
 *  Kept as the public `references` shape so the chat surface's `references` prop
 *  still type-checks. */
export type ReferenceActions = ReferenceActionsState;

/** Load-once hover preview for a blob reference, plus the resolved renderer. The
 *  fetch is keyed on the handle and fires at most once per chip/row mount. */
function useBlobPreview(reference: BlobReference) {
  const fetchBlob = useReferenceActions((state) => state.fetchBlob);
  const [preview, setPreview] = useState<PreviewState>({ status: "idle" });
  const requested = useRef(false);

  const loadPreview = useCallback(() => {
    if (requested.current || !fetchBlob) return;
    requested.current = true;
    setPreview({ status: "loading" });
    fetchBlob(reference.handle, 0, PREVIEW_PAGE).then(
      (page) => setPreview({ status: "ready", page }),
      (err: unknown) =>
        setPreview({ status: "error", message: err instanceof Error ? err.message : "Couldn't load preview" }),
    );
  }, [fetchBlob, reference.handle]);

  // Pick the renderer once we know the page kind; before that, fall back to the
  // declared refType (or text) so the glyph + label are stable.
  const page = preview.status === "ready" ? preview.page : null;
  const renderer = resolveReferenceRenderer(reference.refType, page?.kind ?? reference.refType ?? "text");
  return { fetchBlob, preview, loadPreview, renderer, page };
}

/** The content shown inside the anchored preview panel. */
function PreviewBody({ preview, renderer }: { preview: PreviewState; renderer: ReferenceRenderer }): ReactNode {
  if (preview.status === "ready") {
    return (
      <>
        <span className="alk-refstore-preview__meta">{renderer.describe(preview.page)}</span>
        <span className="alk-refstore-preview__body">{renderer.Preview({ page: preview.page })}</span>
      </>
    );
  }
  if (preview.status === "error") return <span className="alk-refstore-preview__meta">{preview.message}</span>;
  return <span className="alk-refstore-preview__meta">Loading preview…</span>;
}

/** The anchored hover/focus preview. Rides the base Popover in hover mode
 *  (card-ground floating surface, portaled, animated, viewport-flipping). The trigger
 *  render-prop passes the popover's hover/focus props through; the CLICK toggle
 *  is deliberately dropped by the callers (a chip's own click opens the blob in
 *  the editor — the preview is a hover/focus read-out only). */
function PreviewPopover({
  reference,
  preview,
  renderer,
  children,
}: {
  reference: BlobReference;
  preview: PreviewState;
  renderer: ReferenceRenderer;
  children: (props: PopoverTriggerProps) => ReactNode;
}) {
  return (
    <Popover
      openOn="hover"
      side="top"
      clampHeight={false}
      label={`Preview of ${reference.name}`}
      panelClassName="alk-refstore-preview"
      padding="var(--alkSpace2) var(--alkSpace3)"
      trigger={children}
    >
      <PreviewBody preview={preview} renderer={renderer} />
    </Popover>
  );
}

interface ReferenceChipProps {
  reference: BlobReference;
}

/** Render an inline reference chip; host side effects come from the store. The
 *  hover preview rides the anchored Popover so it never runs off-screen. */
export function ReferenceChip({ reference }: ReferenceChipProps) {
  const onOpen = useReferenceActions((state) => state.onOpen);
  const onExport = useReferenceActions((state) => state.onExport);
  const { fetchBlob, preview, loadPreview, renderer } = useBlobPreview(reference);
  const open = onOpen ? () => onOpen(reference) : undefined;

  const chip = (hover?: Omit<PopoverTriggerProps, "onClick">) => (
    <button
      type="button"
      {...hover}
      className="alk-refstore-chip"
      onClick={open}
      disabled={!open}
      aria-label={open ? `Open ${reference.name} in the editor` : reference.name}
      title={reference.name}
    >
      <span className="alk-refstore-chip__icon" aria-hidden>{renderer.icon}</span>
      <span className="alk-refstore-chip__name">{reference.name}</span>
      {open ? <IconExternalLink className="alk-refstore-chip__open" size="var(--alkIconSm)" aria-hidden /> : null}
    </button>
  );

  return (
    <span className="alk-refstore-chip-wrap" onMouseEnter={loadPreview} onFocus={loadPreview}>
      {fetchBlob ? (
        <PreviewPopover reference={reference} preview={preview} renderer={renderer}>
          {({ onClick: _toggle, ...hover }) => chip(hover)}
        </PreviewPopover>
      ) : (
        chip()
      )}
      {onExport ? (
        <Button
          iconOnly
          variant="secondary"
          fill="ghost"
          size="sm"
          className="alk-refstore-chip__export"
          onClick={() => onExport(reference)}
          aria-label={`Export ${reference.name}`}
          title={`Export ${reference.name}`}
        >
          <IconDownload size="var(--alkIconSm)" aria-hidden />
        </Button>
      ) : null}
    </span>
  );
}

/** A blob reference as a full-width artifact row (the Results page): a clickable
 *  name + summary that opens the full blob, an always-visible download, and the
 *  same anchored hover preview as the chip. */
export function ReferenceRow({ reference }: ReferenceChipProps) {
  const onOpen = useReferenceActions((state) => state.onOpen);
  const onExport = useReferenceActions((state) => state.onExport);
  const { fetchBlob, preview, loadPreview, renderer, page } = useBlobPreview(reference);
  const open = onOpen ? () => onOpen(reference) : undefined;
  const detail = page ? renderer.describe(page) : reference.refType ?? reference.mime ?? "";

  const main = (hover?: Omit<PopoverTriggerProps, "onClick">) => (
    <button
      type="button"
      {...hover}
      className="alk-refstore-row__main"
      onClick={open}
      disabled={!open}
      aria-label={open ? `Open ${reference.name} in the editor` : reference.name}
      title={reference.name}
    >
      <span className="alk-refstore-row__icon" aria-hidden>{renderer.icon}</span>
      <span className="alk-refstore-row__text">
        <span className="alk-refstore-row__name">{reference.name}</span>
        {detail ? <span className="alk-refstore-row__detail">{detail}</span> : null}
      </span>
      {open ? <IconExternalLink className="alk-refstore-row__open" size="var(--alkIconSm)" aria-hidden /> : null}
    </button>
  );

  return (
    <div className="alk-refstore-row" onMouseEnter={loadPreview} onFocus={loadPreview}>
      {fetchBlob ? (
        <PreviewPopover reference={reference} preview={preview} renderer={renderer}>
          {({ onClick: _toggle, ...hover }) => main(hover)}
        </PreviewPopover>
      ) : (
        main()
      )}
      {onExport ? (
        <Button
          iconOnly
          variant="secondary"
          fill="ghost"
          size="sm"
          style={{ flex: "0 0 auto" }}
          onClick={() => onExport(reference)}
          aria-label={`Download ${reference.name}`}
          title={`Download ${reference.name}`}
        >
          <IconDownload size="var(--alkIconLg)" aria-hidden />
        </Button>
      ) : null}
    </div>
  );
}

export interface ReferenceListProps {
  references: BlobReference[] | undefined;
  /** "card": the attachment band inside a tool card. "list": vertical artifact
   *  rows (the Results page). Default: a wrapping row of chips beneath a card. */
  variant?: "card" | "list";
}

/** Render blob references. Empty → nothing. */
export function ReferenceList({ references, variant }: ReferenceListProps) {
  if (!references || references.length === 0) return null;
  if (variant === "list") {
    return (
      <div className="alk-refstore-rows" aria-label="Results">
        {references.map((reference) => (
          <ReferenceRow key={reference.handle} reference={reference} />
        ))}
      </div>
    );
  }
  const className = variant === "card" ? "alk-refstore-list is-card" : "alk-refstore-list";
  return (
    <div className={className} aria-label="Results">
      {references.map((reference) => (
        <ReferenceChip key={reference.handle} reference={reference} />
      ))}
    </div>
  );
}

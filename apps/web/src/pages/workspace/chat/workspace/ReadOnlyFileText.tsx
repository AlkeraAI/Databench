// A file's text from the drive, read only: what a view that cannot open its
// file live (a notebook the server will not open as one) shows instead of an
// empty pane, drawn by the same preview surface a file tab uses.

import { useCallback, useMemo, useRef } from "react";

import { PreviewSurface } from "@alkera/ui";

import { useMintContentGrant, type Item, type MintContentGrant } from "@/api/files";
import { contentUrl, downloadItem } from "@/lib/files/download";
import { previewFacts, usePreviewContent } from "@/pages/workspace/files/preview/usePreviewContent";

import { readOnlyRendererFor } from "./fileViewers";

export interface ReadOnlyFileTextProps {
  driveId: string;
  item: Item;
}

export function ReadOnlyFileText({ driveId, item }: ReadOnlyFileTextProps) {
  // The bytes hook keys its fetch on the mint it is handed: a stable one buys
  // the file once.
  const mutation = useMintContentGrant();
  const latest = useRef(mutation.mutateAsync);
  latest.current = mutation.mutateAsync;
  const mint = useCallback<MintContentGrant>((vars) => latest.current(vars), []);
  const facts = useMemo(() => previewFacts(item), [item]);
  const rendererId = readOnlyRendererFor(facts);
  const bytes = usePreviewContent(item, mint, rendererId);
  const download = useCallback(() => downloadItem(contentUrl(driveId, item.id), item), [driveId, item]);
  return <PreviewSurface {...bytes} rendererId={rendererId} onDownload={download} />;
}

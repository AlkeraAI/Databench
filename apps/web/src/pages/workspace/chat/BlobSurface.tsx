import { BlobView, Button, StackedPage } from "@alkera/ui";
import type { BlobFetcher, BlobReference } from "@alkera/chat-model";
import { IconDownload } from "@tabler/icons-react";
import { useMemo, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import { useStackedBack } from "./crumbTrail";
import { StackedCrumbs } from "./StackedCrumbs";
import { chatData, EXPORT_TRUNCATED_NOTICE, exportBlob } from "./data";

/**
 * The full, READ-ONLY, paginated view of a tool-result blob — opened from a
 * reference chip in the chat. A stacked page (like the compaction / plan detail
 * surfaces); the paging + rendering live in `<BlobView>`, which walks the
 * daemon's `blob.fetch` pages (rows by row, text by character).
 */
export function BlobSurface() {
  return (
    <BlobBody />
  );
}

function BlobBody() {
  const { chatId, handle } = useParams();
  const location = useLocation();
  const search = new URLSearchParams(location.search);
  const name = search.get("name")?.trim() || "Result";
  const refType = search.get("type")?.trim() || undefined;
  const mime = search.get("mime")?.trim() || undefined;
  // Panel mode = this route IS its own editor tab (opened via the alkera.openBlob
  // command), not a subpage pushed onto the chat — so it names itself instead of
  // carrying a trail there is no stack behind.
  const isPanel = search.get("panel") === "1";
  const back = useStackedBack();
  const reference: BlobReference = useMemo(
    () => ({ handle: handle ?? "", name, refType, mime }),
    [handle, name, refType, mime],
  );
  const fetchBlob: BlobFetcher = (blobHandle, offset, limit) => chatData().fetchBlob(blobHandle, offset, limit);
  // A download that stopped short of the result says so beside the button that
  // started it. Silence here sent an analyst away with a prefix of their answer.
  const [truncated, setTruncated] = useState(false);
  const actions = handle ? (
    <Button
      iconOnly
      variant="secondary"
      fill="ghost"
      size="sm"
      aria-label="Download this result"
      title="Download"
      onClick={() => {
        void exportBlob(reference).then((result) => setTruncated(result.truncated));
      }}
    >
      <IconDownload size="var(--alkIconMd)" aria-hidden />
    </Button>
  ) : null;

  return (
    <StackedPage
      title={isPanel ? name : undefined}
      subtitle={isPanel ? chatId : undefined}
      nav={isPanel ? undefined : <StackedCrumbs current={name} />}
      onBack={isPanel ? undefined : back}
      actions={actions}
    >
      {truncated ? <p role="status">{EXPORT_TRUNCATED_NOTICE}</p> : null}
      {handle ? (
        <BlobView reference={reference} fetchBlob={fetchBlob} />
      ) : (
        <div role="alert">Result not found.</div>
      )}
    </StackedPage>
  );
}

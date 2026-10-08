import {
  EmptyState,
  ReferenceList,
  ReferenceStoreProvider,
  StackedPage,
} from "@alkera/ui";
import type { BlobReference } from "@alkera/chat-model";
import { IconTable } from "@tabler/icons-react";
import { useCallback, useMemo, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { collectBlobs } from "./blobModel";
import { chatRoutes } from "./chatRoutes";
import { stackedTarget } from "./controller/useChatNavigation";
import { useStackedBack } from "./crumbTrail";
import { StackedCrumbs } from "./StackedCrumbs";
import { chatData, chatHost, EXPORT_TRUNCATED_NOTICE, exportBlob } from "./data";
import { useTranscriptLookup } from "./useTranscriptLookup";

/**
 * The per-chat Results page: every large tool result (blob) this chat produced,
 * listed as rich preview cards. Each card opens the full paginated blob in the
 * editor and offers a Download — the same affordances as the inline cards,
 * gathered in one place.
 */
export function BlobsSurface() {
  return (
    <BlobsBody />
  );
}

function BlobsBody() {
  const { chatId } = useParams();
  const id = chatId ?? "";
  const back = useStackedBack();
  const navigate = useNavigate();
  const location = useLocation();

  // Every result the chat produced, not only the ones in the page it opened on:
  // this list has no other way to reach a table from Tuesday, so it reads the
  // transcript to its start (bounded by the lookup's page budget).
  const lookup = useTranscriptLookup(id ? id : null);
  const blobs = useMemo(() => collectBlobs(lookup.turns), [lookup.turns]);

  const fetchBlob = useCallback(
    (handle: string, offset: number, limit?: number) => chatData().fetchBlob(handle, offset, limit),
    [],
  );
  // Open the result in its own editor tab (like the inline chat cards do), not a
  // nested subpage — one tab per handle, owned by the host command. A browser
  // has no tabs to own, so there the same surface stacks on this page.
  const onOpen = useCallback(
    (reference: BlobReference) => {
      if (chatHost().kind !== "vscode") {
        navigate(
          stackedTarget(chatRoutes().blob(id, reference), `${location.pathname}${location.search}`),
        );
        return;
      }
      void chatHost().runCommand({
        command: "alkera.openBlob",
        args: {
          chatId: id,
          handle: reference.handle,
          name: reference.name,
          refType: reference.refType,
          mime: reference.mime,
        },
      });
    },
    [id, location.pathname, location.search, navigate],
  );

  // A download that stopped short of the result says so on the page that
  // offered it. This list is a download affordance in its own right — a row's
  // button saves the file without ever opening the result — so silence here
  // sent an analyst away with a prefix of their answer and nothing marking it.
  const [truncated, setTruncated] = useState(false);
  const actions = useMemo(
    () => ({
      fetchBlob,
      onOpen,
      onExport: (reference: BlobReference) => {
        void exportBlob(reference).then((result) => setTruncated(result.truncated));
      },
    }),
    [fetchBlob, onOpen],
  );

  return (
    <StackedPage nav={<StackedCrumbs current="Results" />} onBack={back}>
      {lookup.loading || lookup.searching ? (
        <div role="status">Loading results…</div>
      ) : blobs.length === 0 ? (
        <EmptyState
          size="md"
          icon={<IconTable size={44} stroke={1.25} />}
          title="No results yet"
          body="Large tables, text, or files from tool calls collect here to open or download."
        />
      ) : (
        <ReferenceStoreProvider actions={actions}>
          {truncated ? <p role="status">{EXPORT_TRUNCATED_NOTICE}</p> : null}
          <ReferenceList references={blobs} variant="list" />
        </ReferenceStoreProvider>
      )}
    </StackedPage>
  );
}

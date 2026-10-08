// The notebook preview: the notebook as stored, drawn read-only with the
// editor's own cells and outputs, and its source a switch away. Nothing here
// edits, runs, starts a kernel or joins a live channel: the cells and saved
// outputs come from one read of the stored route.

import { EMPTY_RUNTIME, NotebookReader, type CellRuntime, type DocCell } from "@alkera/notebook-ui";
import "@alkera/notebook-ui/styles";
import { PreviewNotice, SegmentedControl, previewRendererById, type PreviewProps } from "@alkera/ui";
import { useMemo, useState, type ReactElement } from "react";

import { refusalSentence } from "@/api/errors";
import { outputBlobUrl, useStoredNotebook, type StoredNotebook } from "@/api/notebooks";
import { cellOfView } from "@/pages/workspace/chat/workspace/notebook/notebookWire";
import { outputsOf } from "@/pages/workspace/chat/workspace/notebook/notebookRuntime";
import "@/pages/workspace/chat/workspace/notebook/outputRenderers";
import { useHostDark } from "@/pages/workspace/chat/useHostDark";

import "./notebookPreview.css";

/** The views, named as the editor tab names them. */
export const NOTEBOOK_LABEL = "Notebook";
export const SOURCE_LABEL = "Source";
export const NO_OUTPUTS = "No saved outputs";
const LOADING = "Loading notebook…";
/** The renderer the source is shown with: the one a Python file gets. */
const SOURCE_RENDERER = "code";

type View = "notebook" | "source";

const OPTIONS = [
  { key: "notebook", label: NOTEBOOK_LABEL },
  { key: "source", label: SOURCE_LABEL },
] as const;

interface Shown {
  cells: DocCell[];
  runtime: Record<string, CellRuntime>;
  hasOutputs: boolean;
}

/** The stored notebook as the reader draws it. Saved outputs are shown as
 *  outputs, not marked as "not run in this kernel": there is no kernel here. */
function shownOf(stored: StoredNotebook): Shown {
  const cells: DocCell[] = [];
  const runtime: Record<string, CellRuntime> = {};
  let hasOutputs = false;
  for (const cell of stored.cells) {
    cells.push({ id: cell.id, kind: cell.kind, name: cell.name, source: cell.source ?? "", config: cell.config ?? {}, meta: cell.meta ?? {} });
    const outputs = outputsOf(cell.id, cell.outputs) ?? [];
    hasOutputs ||= outputs.length > 0;
    runtime[cell.id] = { ...cellOfView(EMPTY_RUNTIME, cell), outputs, saved: false, outdated: false };
  }
  return { cells, runtime, hasOutputs };
}

function Source(props: PreviewProps): ReactElement | null {
  const renderer = previewRendererById(SOURCE_RENDERER);
  return renderer ? <renderer.Component {...props} /> : null;
}

export default function NotebookPreview(props: PreviewProps): ReactElement {
  const [view, setView] = useState<View>("notebook");
  const file = props.file;
  return (
    <div className="alk-nb-preview">
      <div className="alk-nb-preview__bar">
        <SegmentedControl options={OPTIONS} value={view} onChange={(key) => setView(key as View)} label="View" size="sm" />
      </div>
      <div className="alk-nb-preview__body">
        {view === "source" || file === undefined ? <Source {...props} /> : <Stored {...props} driveId={file.driveId} itemId={file.itemId} />}
      </div>
    </div>
  );
}

function Stored(props: PreviewProps & { driveId: string; itemId: string }): ReactElement {
  const { driveId, itemId, version } = props;
  const stored = useStoredNotebook(driveId, itemId, version);
  const dark = useHostDark();
  const shown = useMemo(() => (stored.data ? shownOf(stored.data) : null), [stored.data]);
  const output = useMemo(
    () => ({ theme: dark ? ("dark" as const) : ("light" as const), blobUrl: (sha: string) => outputBlobUrl(driveId, itemId, sha) }),
    [dark, driveId, itemId],
  );
  if (stored.isError) {
    return (
      <>
        <PreviewNotice>{refusalSentence(stored.error)}</PreviewNotice>
        <Source {...props} />
      </>
    );
  }
  if (shown === null) return <PreviewNotice>{LOADING}</PreviewNotice>;
  return (
    <>
      {shown.hasOutputs ? null : <p className="alk-nb-preview__note">{NO_OUTPUTS}</p>}
      <NotebookReader cells={shown.cells} runtime={shown.runtime} output={output} />
    </>
  );
}

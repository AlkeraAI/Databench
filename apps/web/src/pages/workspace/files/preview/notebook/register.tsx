// A notebook (`.alknb.py`) previews as a notebook, wherever a file previews:
// the Files page modal, the page a shared link lands on, a tab's preview. One
// renderer in the shared preview registry, matched through the file-type
// owner, so no surface decides on its own what a notebook looks like.
//
// It asks the host for the file's text (the Source view, and what is shown
// when the notebook cannot be read) and reads the cells and saved outputs
// through the notebook's stored route, under the same decision as the bytes.
// The notebook editor and its outputs load only when a notebook is previewed.

import { PreviewNotice, registerPreviewRenderer, type PreviewProps, type PreviewRenderer } from "@alkera/ui";
import { lazy, Suspense, type ReactElement } from "react";

import { isNotebookName } from "@/lib/files/fileTypes";

/** The notebook renderer's id in the preview registry. */
export const NOTEBOOK_PREVIEW = "notebook";

const NotebookPreview = lazy(() => import("./NotebookPreview"));

function LazyNotebookPreview(props: PreviewProps): ReactElement {
  return (
    <Suspense fallback={<PreviewNotice>Loading preview…</PreviewNotice>}>
      <NotebookPreview {...props} />
    </Suspense>
  );
}

export const notebookPreviewRenderer: PreviewRenderer = {
  id: NOTEBOOK_PREVIEW,
  // Over the source and Markdown readers: a notebook is a Python file
  // underneath, and must not be drawn as one.
  priority: 60,
  match: (facts) => isNotebookName(facts.name),
  needs: () => "text",
  Component: LazyNotebookPreview,
};

export function registerNotebookPreview(): void {
  registerPreviewRenderer(notebookPreviewRenderer);
}

registerNotebookPreview();

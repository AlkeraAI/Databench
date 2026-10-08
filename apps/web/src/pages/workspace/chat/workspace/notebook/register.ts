// Notebooks (`.alknb.py`) open as a notebook, with their source a toggle away.
// The source view is the live text editor on the file itself: what is typed
// there reaches the notebook through the server's merge of the file into the
// live notebook document.

import { lazy } from "react";

import { isNotebookName } from "@/lib/files/fileTypes";

import { EDIT_VIEW, registerFileViewer, type FileViewProps } from "../fileViewers";

export { isNotebookName };

export const NOTEBOOK_VIEW = "notebook";

const NotebookTab = lazy(() => import("./NotebookTab")) as unknown as React.ComponentType<FileViewProps>;

export function registerNotebookViewer(): void {
  registerFileViewer({
    id: "notebook",
    priority: 50,
    match: (facts) => isNotebookName(facts.name),
    views: [
      { id: NOTEBOOK_VIEW, role: "preview", label: "Notebook", draw: { kind: "custom", Component: NotebookTab } },
      { id: EDIT_VIEW, role: "edit", label: "Source", draw: { kind: "live-editor" } },
    ],
  });
}

registerNotebookViewer();

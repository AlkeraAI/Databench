// A notebook tool's ask, compact: one row per cell the agent asked to run (its
// name and the first lines of its code), the cells that run with them folded
// behind one quiet line, the packages an install would add, and why it needs
// approval, said once. Every word arrives from the shared presentation; this
// file only lays it out.

import { useState, type ReactNode } from "react";

import type { PresentedNotebook, PresentedNotebookCell } from "@alkera/chat-model";

import "./notebookAsk.css";

/** The card's title for a notebook ask: the action, then the notebook (a link
 *  where the host can open a file), then whatever follows it. */
export function NotebookTitle({
  notebook,
  onOpenFile,
}: {
  notebook: PresentedNotebook;
  onOpenFile?: (path: string) => void;
}): ReactNode {
  return (
    <>
      {notebook.lead}{" "}
      {onOpenFile ? (
        <button
          type="button"
          className="chat-notebook-ask__file"
          title={notebook.filePath}
          onClick={() => onOpenFile(notebook.filePath)}
        >
          {notebook.fileName}
        </button>
      ) : (
        <span className="chat-notebook-ask__file" title={notebook.filePath}>
          {notebook.fileName}
        </span>
      )}
      {notebook.tail}
    </>
  );
}

function CellRows({ cells, label }: { cells: PresentedNotebookCell[]; label: string }): ReactNode {
  return (
    <ul className="chat-notebook-ask__cells" aria-label={label}>
      {cells.map((cell, index) => (
        <li key={`${index}:${cell.name}`} className="chat-notebook-ask__cell">
          <span className="chat-notebook-ask__name">{cell.name}</span>
          {cell.previewLines.length > 0 ? (
            <code className="chat-notebook-ask__code">
              {cell.previewLines.map((line, n) => (
                <span key={n} className="chat-notebook-ask__line">
                  {line}
                </span>
              ))}
            </code>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/** The body of a notebook ask, in the subject's place. */
export function NotebookAsk({ notebook }: { notebook: PresentedNotebook }): ReactNode {
  const [open, setOpen] = useState(false);
  return (
    <div className="chat-notebook-ask">
      {notebook.cells.length > 0 ? <CellRows cells={notebook.cells} label="Cells to run" /> : null}
      {notebook.relatedLine && notebook.related.length > 0 ? (
        <>
          <button
            type="button"
            className="chat-notebook-ask__more"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {notebook.relatedLine}
          </button>
          {open ? <CellRows cells={notebook.related} label={notebook.relatedLine} /> : null}
        </>
      ) : null}
      {notebook.packages.length > 0 ? (
        <ul className="chat-notebook-ask__packages" aria-label="Packages">
          {notebook.packages.map((name) => (
            <li key={name}>{name}</li>
          ))}
        </ul>
      ) : null}
      {notebook.reason ? <p className="chat-notebook-ask__reason">{notebook.reason}</p> : null}
    </div>
  );
}

// A new notebook file: the text `alkera-notebook new` writes, uploaded into a
// folder through the same Files upload sessions the pane's uploads use.

import { UploadClient, type UploadClientOptions } from "@/api/filesUpload";
import { newCellId } from "@/api/realtime/crdt/notebookDoc";

import { notebookFileName } from "./notebookNames";

/** The marimo version the format's writer records. */
const GENERATED_WITH = "0.25.1";

/** The ids a new notebook's two cells are written with. */
export interface NewNotebookIds {
  setup: string;
  cell: string;
}

function freshIds(): NewNotebookIds {
  return { setup: newCellId(), cell: newCellId() };
}

/** A new notebook in format 1.0: polars frames, the setup block importing
 *  `alkera` (Markdown and SQL cells call into it, and the import is what lets
 *  the file run outside Alkera), and one empty cell. Each id is 10 characters
 *  of Crockford base32 from 50 random bits. The same text `alkera-notebook
 *  new` writes; both are tested against
 *  packages/alkera-notebook/tests/vectors/new_notebook.json. */
export function newNotebookText(ids: NewNotebookIds = freshIds()): string {
  return [
    "# >>> alkera",
    '# format = "1.0"',
    '# dataframe = "polars"',
    "# <<< alkera",
    "",
    "import marimo",
    "",
    `__generated_with = "${GENERATED_WITH}"`,
    "app = marimo.App()",
    "",
    `with app.setup(alkera_id="${ids.setup}"):`,
    "    import alkera",
    "",
    "",
    `@app.cell(alkera_id="${ids.cell}")`,
    "def _():",
    "    return",
    "",
    "",
    'if __name__ == "__main__":',
    "    app.run()",
    "",
  ].join("\n");
}

export interface CreatedNotebook {
  nodeId: string;
  name: string;
}

/** Upload a new notebook named for `typed` into `parentId`. A name already
 *  taken gets the server's "keep both" rename. Rejects with the upload's
 *  error when it fails. */
export async function createNotebook(
  driveId: string,
  parentId: string,
  typed: string,
  options: Pick<UploadClientOptions, "fetchImpl" | "digest"> & { ids?: NewNotebookIds } = {},
): Promise<CreatedNotebook> {
  const name = notebookFileName(typed);
  const client = new UploadClient({ driveId, ...(options.fetchImpl ? { fetchImpl: options.fetchImpl } : {}), ...(options.digest ? { digest: options.digest } : {}) });
  const file = new File([newNotebookText(options.ids)], name, { type: "text/x-python" });
  const handle = await client.start(file, parentId, { conflictBehavior: "rename" });
  const done = await handle.done();
  if (done.state !== "done") throw done.error ?? new Error("The notebook was not created.");
  if (!done.result) throw new Error("The notebook was not created.");
  return { nodeId: done.result.nodeId, name };
}

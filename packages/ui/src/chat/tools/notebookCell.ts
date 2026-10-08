// The one way a notebook step offers to take the reader to a cell.
import type { StepAction, StepEnvironment } from "./step";

/** A cell id as the notebook format writes it. Anything else (a name, a
 *  position) is not stable enough to anchor a link to. */
const CELL_ID = /^[0-9a-hjkmnp-tv-z]{10}$/;

/** "Go to cell" for a cell of the notebook at `path`, where the shell can open
 *  one and the call named a cell by its stable id; otherwise nothing. */
export function goToCell(
  env: StepEnvironment | undefined,
  path: string,
  cellId: string,
  cellName: string,
): StepAction | undefined {
  const open = env?.onOpenNotebookCell;
  if (!open || !path || !CELL_ID.test(cellId)) return undefined;
  return {
    label: "Go to cell",
    ariaLabel: cellName ? `Go to cell ${cellName}` : undefined,
    onAction: () => open(path, cellId),
  };
}

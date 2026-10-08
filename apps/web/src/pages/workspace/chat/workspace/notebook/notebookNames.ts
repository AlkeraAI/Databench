// What a new notebook file is called. Kept apart from the code that writes
// one, so the Files pane can name a notebook without loading it.

import { NOTEBOOK_SUFFIX } from "@/lib/files/fileTypes";

export { NOTEBOOK_SUFFIX };
/** The name a notebook gets when none is typed. */
export const DEFAULT_NOTEBOOK_NAME = "Untitled";

/** The file name for what was typed: the notebook suffix added when it is not
 *  there already, and the default name for nothing. */
export function notebookFileName(typed: string): string {
  const name = typed.trim() || DEFAULT_NOTEBOOK_NAME;
  if (name.toLowerCase().endsWith(NOTEBOOK_SUFFIX)) return name;
  // "report.py" becomes "report.alknb.py", not "report.py.alknb.py".
  const stem = name.toLowerCase().endsWith(".py") ? name.slice(0, -3) : name;
  return `${stem}${NOTEBOOK_SUFFIX}`;
}

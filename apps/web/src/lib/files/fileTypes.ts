// File types the portal names itself, matched on the longest suffix first: an
// Alkera notebook is a Python file underneath, so ".alknb.py" must win before
// ".py" would. The one place a compound suffix like this is spelled.

export interface FileType {
  /** The suffix, lowercase, leading dot included. */
  suffix: string;
  /** What the Kind column reads. */
  label: string;
}

/** The suffix every notebook file carries. */
export const NOTEBOOK_SUFFIX = ".alknb.py";

export const FILE_TYPES: readonly FileType[] = [{ suffix: NOTEBOOK_SUFFIX, label: "Notebook" }];

/** The named type a file has, by the longest suffix that matches, or null. */
export function fileTypeOf(name: string): FileType | null {
  const lower = name.toLowerCase();
  let best: FileType | null = null;
  for (const type of FILE_TYPES) {
    if (lower.endsWith(type.suffix) && lower.length > type.suffix.length && (best === null || type.suffix.length > best.suffix.length)) {
      best = type;
    }
  }
  return best;
}

/** Whether a file is a notebook, by its name. */
export function isNotebookName(name: string): boolean {
  return fileTypeOf(name)?.suffix === NOTEBOOK_SUFFIX;
}

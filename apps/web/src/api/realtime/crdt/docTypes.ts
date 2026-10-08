// The live document types this build can hold, and the root text each keeps
// its content in. The server's registry (`backend/services/crdt/registry.py`)
// is the authority on what it serves; this is the browser's half, so a new
// type is one entry here plus a binding for its editor.

export const LIVE_DOC_TYPES = {
  /** A chat's shared composer draft. */
  chat_draft: { text: "draft" },
  /** A text file co-edited live, keyed by its node id. */
  file: { text: "content" },
  /** A notebook, keyed by its file's node id: cells, not one root text. */
  notebook: { text: null },
} as const;

export type LiveDocType = keyof typeof LIVE_DOC_TYPES;

/** The root text `docType` keeps its content in. A type whose content is not
 * one root text (a notebook) has none, and asking for it is a bug. */
export function contentTextOf(docType: LiveDocType): string {
  const text = LIVE_DOC_TYPES[docType].text;
  if (text === null) throw new Error(`${docType} keeps no single content text`);
  return text;
}

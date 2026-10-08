/**
 * What a preview offers, as data.
 *
 * Three surfaces show a file — the Files page's large preview, the chat's file
 * tab, and the page a share link lands on — and each one had hand-written its
 * own row of buttons. They had already drifted: one said "Open in Files", the
 * other "Reveal in Files"; one had a Close and the other did not; one offered
 * Download for a file whose downloads are off. A reader moving between them was
 * being told the same bytes could do different things.
 *
 * So the set is a table, and a surface renders the table rather than deciding
 * again. The table says WHAT is offered and in what order; the surface supplies
 * the handlers and the styling, which are the only parts that are really its
 * own.
 *
 * Soft wrap is in the set and is not a host's to draw: it is a view control
 * that belongs to whichever renderer supports it (a text or code preview), so
 * it is listed with `owner: "renderer"` and never comes back from
 * `previewActionsFor`. A host that drew it too would put two toggles for one
 * setting on the same screen.
 */

export type PreviewActionId =
  | "download"
  | "open-in-files"
  | "share"
  | "copy"
  | "copy-link"
  | "open-new-tab"
  | "soft-wrap"
  | "close";

export interface PreviewAction {
  id: PreviewActionId;
  label: string;
  /** Who draws it: the surface around the bytes, or the renderer drawing them. */
  owner: "host" | "renderer";
}

/** The whole set, in the order a surface lays it out. */
export const PREVIEW_ACTIONS: readonly PreviewAction[] = [
  { id: "download", label: "Download", owner: "host" },
  { id: "open-in-files", label: "Open in Files", owner: "host" },
  { id: "share", label: "Share…", owner: "host" },
  // The bytes as a reader would paste them: text as text, an image as an
  // image. Offered only where such a form exists (`copyableKind`).
  { id: "copy", label: "Copy", owner: "host" },
  { id: "copy-link", label: "Copy link", owner: "host" },
  { id: "open-new-tab", label: "Open in new tab", owner: "host" },
  // Drawn by the text and code renderers, where a long line is a real choice.
  { id: "soft-wrap", label: "Soft wrap", owner: "renderer" },
  { id: "close", label: "Close", owner: "host" },
];

export interface PreviewActionState {
  /** Downloads are off for this row: there are no bytes to hand over. */
  sealed: boolean;
  /** The bytes can be fetched at all — not sealed, not in the trash. */
  reachable: boolean;
  /** The surface has a folder to show the file in. A file shared alone does not. */
  canOpenInFiles: boolean;
  /** The surface can open the sharing dialog. */
  canShare: boolean;
  /** The content on screen has a paste-able form: text, or an image. */
  canCopy: boolean;
}

/** Whether one action applies in this state. Close always does: a reader must
 *  never be left in a preview with no way out of it. */
function applies(id: PreviewActionId, state: PreviewActionState): boolean {
  switch (id) {
    case "download":
      return !state.sealed;
    case "open-new-tab":
      return state.reachable;
    case "open-in-files":
      return state.canOpenInFiles;
    case "share":
      return state.canShare;
    case "copy":
      return state.canCopy;
    // A link is an address, not the bytes: a row somebody may see, they may
    // hand on, whether or not anything can be fetched.
    case "copy-link":
    case "close":
      return true;
    case "soft-wrap":
      return false;
  }
}

/** The actions this surface draws, in order. */
export function previewActionsFor(state: PreviewActionState): PreviewAction[] {
  return PREVIEW_ACTIONS.filter(
    (action) => action.owner === "host" && applies(action.id, state),
  );
}

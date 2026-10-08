// What a file tab can show a file AS.
//
// A tab is a file and a way of looking at it: the source of a Markdown note or
// the note rendered, an HTML report's markup or the page it draws, a CSV as the
// table it is or the text it is made of. Which ways a type offers, and which it
// opens in, is decided here once: the tab, the view toggle, "Open preview to
// the side" and the keyboard all ask this registry rather than knowing a type.
// A file opens in its viewer's preview when the viewer offers one, so a note,
// a page or a table is first seen as what it draws; edit is the toggle.
//
// A viewer registers itself the way a preview renderer or a tab kind does, so a
// later one (a notebook, a dashboard, a lineage graph) is a registration, not a
// branch in the tab: it matches its files, names its views, and draws each one
// either declaratively (the live editor, or a preview renderer by id) or with a
// component of its own.

import type { ComponentType } from "react";

import { previewKindFor, type PreviewFacts, type PreviewKind } from "@alkera/ui";

import type { Item } from "@/api/files";

import type { WorkspaceCtx, WorkspaceTab } from "./tabKinds";

/** What a view is FOR. The toggle and the preview shortcuts reason about roles,
 *  so a viewer can call its views anything and still be switched between. */
export type ViewRole = "edit" | "preview";

/** The view ids the built-in viewers use. A viewer may register others. */
export const EDIT_VIEW = "edit";
export const PREVIEW_VIEW = "preview";

/** What a custom view is handed. */
export interface FileViewProps {
  tab: WorkspaceTab;
  ctx: WorkspaceCtx;
  item: Item;
  /** Something the reader typed changed the file: the tab stops being a preview. */
  onEdit(): void;
}

/** How the tab draws one view.
 *
 *  - `live-editor`: the file's text, co-edited live. When it cannot be (no
 *    live socket here, the server will not open it: too large, not text) the
 *    text is shown read-only instead.
 *  - `render`: a preview renderer, by id (unset: whichever the type gets).
 *    `live` draws from the live document's text while it is open, so the view
 *    moves as people type; otherwise the drive's copy is drawn, and redrawn as
 *    each edit is written back.
 *  - `custom`: a component of the viewer's own. */
export type ViewDraw =
  | { kind: "live-editor" }
  | { kind: "render"; renderer?: string; live?: boolean }
  | { kind: "custom"; Component: ComponentType<FileViewProps> };

export interface FileViewDef {
  id: string;
  role: ViewRole;
  /** The toggle's label for it. */
  label: string;
  draw: ViewDraw;
}

export interface FileViewer {
  id: string;
  /** Higher wins a file two viewers both match. A tie goes to the newer one. */
  priority: number;
  match(facts: PreviewFacts, kind: PreviewKind): boolean;
  /** In toggle order. Never empty. A file opens in the first preview among
   *  them, or in the first view when none previews. */
  views: readonly FileViewDef[];
}

const viewers = new Map<string, FileViewer>();

/** Teach the workspace a way of showing files. Registering an id again
 *  replaces the earlier viewer. */
export function registerFileViewer(viewer: FileViewer): void {
  if (viewer.views.length === 0) throw new Error(`viewer ${viewer.id} offers no views`);
  viewers.delete(viewer.id);
  viewers.set(viewer.id, viewer);
}

export function unregisterFileViewer(id: string): boolean {
  return viewers.delete(id);
}

/** The viewer for a file. Every file gets one: the catch-all below matches
 *  everything. */
export function fileViewerFor(facts: PreviewFacts): FileViewer {
  const kind = previewKindFor(facts);
  let best: FileViewer | undefined;
  for (const viewer of viewers.values()) {
    if (!viewer.match(facts, kind)) continue;
    if (!best || viewer.priority >= best.priority) best = viewer;
  }
  return best ?? PREVIEW_ONLY;
}

/** The view a file opens in: its viewer's preview when it offers one, else its
 *  first view. */
export function openingView(viewer: FileViewer): FileViewDef {
  return viewer.views.find((view) => view.role === "preview") ?? (viewer.views[0] as FileViewDef);
}

/** The view a tab shows: the one it asked for when the file offers it, else the
 *  view the file opens in. A tab saved by a build that offered a view this one
 *  does not is shown that rather than nothing. */
export function resolveView(viewer: FileViewer, requested: string | null | undefined): FileViewDef {
  const asked = requested ? viewer.views.find((view) => view.id === requested) : undefined;
  return asked ?? openingView(viewer);
}

/** The first view of the other role, for the toggle and its shortcut. */
export function counterpart(viewer: FileViewer, current: FileViewDef): FileViewDef | null {
  const role: ViewRole = current.role === "edit" ? "preview" : "edit";
  return viewer.views.find((view) => view.role === role) ?? null;
}

/** The preview a file offers, if it offers one beside an editor. */
export function previewOf(viewer: FileViewer): FileViewDef | null {
  if (!viewer.views.some((view) => view.role === "edit")) return null;
  return viewer.views.find((view) => view.role === "preview") ?? null;
}

/** Whether the file can be co-edited live in some view. */
export function offersEditing(viewer: FileViewer): boolean {
  return viewer.views.some((view) => view.draw.kind === "live-editor");
}

/** The renderer a file's text is shown read-only with, when the editor cannot
 *  open it: highlighted where it has a grammar, plain where it is prose. */
export function readOnlyRendererFor(facts: PreviewFacts): string {
  const kind = previewKindFor(facts);
  return kind === "code" || kind === "html" || kind === "svg" ? "code" : "text";
}

// -- the built-in viewers --------------------------------------------------------

const EDIT: FileViewDef = { id: EDIT_VIEW, role: "edit", label: "Edit", draw: { kind: "live-editor" } };

const preview = (draw: ViewDraw): FileViewDef => ({
  id: PREVIEW_VIEW,
  role: "preview",
  label: "Preview",
  draw,
});

/** Pictures, documents, media and anything without a reader: drawn, never
 *  edited. Also what an unmatched file falls back to. */
const PREVIEW_ONLY: FileViewer = {
  id: "preview-only",
  priority: -1,
  match: () => true,
  views: [preview({ kind: "render" })],
};

/** Register the viewers the workspace ships with. Runs on import; calling it
 *  again re-registers the same ids. */
export function registerDefaultFileViewers(): void {
  registerFileViewer(PREVIEW_ONLY);

  // Source, config and plain text: an editor and nothing else, as in any editor.
  registerFileViewer({
    id: "text",
    priority: 10,
    match: (_facts, kind) => kind === "code" || kind === "text",
    views: [EDIT],
  });

  // A note's rendering follows the source as it is typed.
  registerFileViewer({
    id: "markdown",
    priority: 20,
    match: (_facts, kind) => kind === "markdown",
    views: [EDIT, preview({ kind: "render", renderer: "markdown", live: true })],
  });

  // A table's rendering follows its text as it is typed.
  registerFileViewer({
    id: "csv",
    priority: 20,
    match: (_facts, kind) => kind === "csv",
    views: [EDIT, preview({ kind: "render", renderer: "csv", live: true })],
  });

  // Markup that draws a page. The rendering is ONLY ever the framed renderer:
  // the page is fetched from the content origin into a sandboxed frame, so what
  // an agent wrote into it never runs in this origin. It is redrawn as edits
  // are written back rather than from the live text, for the same reason.
  registerFileViewer({
    id: "html",
    priority: 20,
    match: (_facts, kind) => kind === "html",
    views: [EDIT, preview({ kind: "render", renderer: "html" })],
  });

  // A drawing's source is markup like any other, and its rendering is framed
  // from the content origin exactly as a page is.
  registerFileViewer({
    id: "svg",
    priority: 20,
    match: (_facts, kind) => kind === "svg",
    views: [EDIT, preview({ kind: "render", renderer: "html" })],
  });
}

registerDefaultFileViewers();

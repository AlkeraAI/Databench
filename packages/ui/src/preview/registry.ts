import { IconFile } from "@tabler/icons-react";
import { createElement, type ReactNode } from "react";

import type { PreviewFacts, PreviewPlan, PreviewProps, PreviewRenderer } from "./types";
import { formatBytes } from "./size";

// The preview-renderer registry. A file is planned once — who draws it, what the
// host must fetch — and every surface that shows files asks the same question, so
// a tab beside a chat, the large modal on the Files page and the page a share link
// lands on can never disagree about a type.
//
// The catch-all fallback is registered here, at module load, so an unknown type is
// a named card with a Download rather than a dead pane. `planPreview` never
// answers `undefined`; with the fallback gone it throws, because a surface that
// silently renders nothing is the bug this registry exists to prevent.

const FALLBACK_PRIORITY = -1;
/** The catch-all renderer's id. Re-register under it to replace the card. */
export const FALLBACK_RENDERER_ID = "fallback";

const renderers = new Map<string, PreviewRenderer>();

/** Teach every preview surface a type. Registering an id again replaces the
 *  earlier renderer and moves it to the front of a priority tie. */
export function registerPreviewRenderer(renderer: PreviewRenderer): void {
  // Delete first: a Map keeps a re-set key in its ORIGINAL position, and a tie is
  // decided by who registered last.
  renderers.delete(renderer.id);
  renderers.set(renderer.id, renderer);
}

/** Drop a renderer. Answers whether one was there. */
export function unregisterPreviewRenderer(id: string): boolean {
  return renderers.delete(id);
}

/** Decide who draws this file and what its bytes must arrive as. Throws only when
 *  the fallback has been unregistered — nothing else can leave a file unplanned. */
export function planPreview(facts: PreviewFacts): PreviewPlan {
  // Bytes that have not arrived cannot be drawn by anyone. Planning the renderer
  // the TYPE deserves would send the host fetching an empty body and leave a
  // broken picture on screen; the card says what is actually happening instead.
  if (facts.synced === false) {
    const fallback = renderers.get(FALLBACK_RENDERER_ID);
    if (!fallback) throw unplanned(facts);
    return { renderer: fallback, need: "none", unsynced: true };
  }

  let best: PreviewRenderer | undefined;
  for (const renderer of renderers.values()) {
    if (!renderer.match(facts)) continue;
    if (!best || renderer.priority >= best.priority) best = renderer;
  }
  if (!best) throw unplanned(facts);

  // Size never decides who draws a file: text arrives in windows and the browser
  // streams pictures, media and framed documents itself.
  return { renderer: best, need: best.needs(facts), unsynced: false };
}

/** The renderer registered under `id`, or `undefined` when none is. */
export function previewRendererById(id: string): PreviewRenderer | undefined {
  return renderers.get(id);
}

/** The plan for drawing this file with one renderer in particular, chosen by
 *  the host rather than by the type: a tab that shows an HTML file as its
 *  rendered page AND as its source asks for each by name. Bytes that have not
 *  arrived still get the card, and an id nobody registered falls back to the
 *  type's own plan rather than to nothing. */
export function planPreviewWith(facts: PreviewFacts, rendererId: string | undefined): PreviewPlan {
  if (rendererId === undefined || facts.synced === false) return planPreview(facts);
  const renderer = renderers.get(rendererId);
  if (!renderer) return planPreview(facts);
  return { renderer, need: renderer.needs(facts), unsynced: false };
}

function unplanned(facts: PreviewFacts): Error {
  return new Error(
    `No preview renderer for ${facts.mime} (${facts.name}) — the fallback renderer is not registered.`,
  );
}


function reasonFor(props: PreviewProps): string {
  switch (props.status) {
    case "gone":
      return "This file is no longer available";
    case "pending":
      return "The machine holding this folder has not written this file back yet";
    case "error":
      return props.error ?? "The preview could not be loaded";
    default:
      return "Preview is not available for this type";
  }
}

/** What a file gets when nothing renders it: its name, what the server says it
 *  is, how big it is, why there is no preview, and the way out — Download. */
function FallbackPreview(props: PreviewProps): ReactNode {
  const { facts } = props;
  return createElement(
    "div",
    { className: "alk-preview-fallback" },
    createElement(IconFile, {
      className: "alk-preview-fallback__glyph",
      size: "var(--alkIconLg)",
      "aria-hidden": true,
    }),
    createElement("p", { className: "alk-preview-fallback__name" }, facts.name),
    createElement(
      "p",
      { className: "alk-preview-fallback__facts" },
      `${facts.mime} · ${formatBytes(facts.size)}`,
    ),
    createElement("p", { className: "alk-preview-fallback__reason" }, reasonFor(props)),
    createElement(
      "button",
      { type: "button", className: "alk-preview-fallback__download", onClick: props.onDownload },
      "Download",
    ),
  );
}

registerPreviewRenderer({
  id: FALLBACK_RENDERER_ID,
  priority: FALLBACK_PRIORITY,
  match: () => true,
  needs: () => "none",
  Component: FallbackPreview,
});

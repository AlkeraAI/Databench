import { registerPreviewRenderer, FALLBACK_RENDERER_ID } from "./registry";
import { BinaryFallback } from "./renderers/BinaryFallback";
import { FramePreview } from "./renderers/FramePreview";
import { IMAGE_VIEW_SETTINGS, ImagePreview, SvgImagePreview } from "./renderers/ImagePreview";
import { MediaPreview } from "./renderers/MediaPreview";
import { TEXT_VIEW_SETTINGS, TextPreview } from "./renderers/TextPreview";
import { codeRenderer } from "./renderers/CodePreview";
import { csvRenderer } from "./renderers/CsvPreview";
import { markdownRenderer } from "./renderers/MarkdownPreview";
import { previewKindFor, type PreviewKind } from "./kind";
import type { PreviewFacts } from "./types";

// The types the library draws out of the box. Importing this module registers
// them, so every surface that renders a preview knows them; a host that wants a
// type drawn its own way registers over the id afterwards and wins.
//
// Every `match` asks `previewKindFor` the one question — what IS this file — so a
// type is classified in a single place rather than once per renderer. The mime the
// SERVER sniffed decides wherever it carries information; where it does not (a
// writer that set no content type leaves `application/octet-stream` on a PNG) the
// name answers, because a picture behind a "not available for this type" card is
// worse than trusting the extension a writer chose.

/** The order a contested file is decided in. The band between a framed document
 *  and plain text is where the readers of structured text belong: a `.md` and a
 *  `.csv` are both `text/plain`-ish and must outrank the raw `<pre>`. */
const PRIORITY = {
  html: 90,
  pdf: 85,
  image: 80,
  svg: 75,
  media: 70,
  text: 10,
  fallback: -1,
} as const;

function isKind(facts: PreviewFacts, ...kinds: readonly PreviewKind[]): boolean {
  return kinds.includes(previewKindFor(facts));
}

/** Teach the registry the built-in types. Runs on import. Calling it again just
 *  re-registers the same ids, which is how the registry already handles a repeat. */
export function registerDefaultPreviewRenderers(): void {
  registerPreviewRenderer({
    id: "html",
    priority: PRIORITY.html,
    match: (facts) => isKind(facts, "html"),
    needs: () => "frame",
    Component: FramePreview,
  });

  registerPreviewRenderer({
    id: "pdf",
    priority: PRIORITY.pdf,
    match: (facts) => isKind(facts, "pdf"),
    needs: () => "frame",
    Component: FramePreview,
  });

  registerPreviewRenderer({
    id: "image",
    priority: PRIORITY.image,
    match: (facts) => isKind(facts, "image"),
    needs: () => "blob",
    viewSettings: IMAGE_VIEW_SETTINGS,
    Component: ImagePreview,
  });

  registerPreviewRenderer({
    id: "svg",
    priority: PRIORITY.svg,
    match: (facts) => isKind(facts, "svg"),
    needs: () => "blob",
    Component: SvgImagePreview,
  });

  registerPreviewRenderer({
    id: "media",
    priority: PRIORITY.media,
    match: (facts) => isKind(facts, "audio", "video"),
    needs: () => "blob",
    Component: MediaPreview,
  });

  registerPreviewRenderer({
    id: "text",
    priority: PRIORITY.text,
    match: (facts) => isKind(facts, "text"),
    needs: () => "text",
    viewSettings: TEXT_VIEW_SETTINGS,
    Component: TextPreview,
  });

  // The registry registers a bare card at load so a plan is never undefined; this
  // is the drawn one, with the glyph for the file's family and the Download.
  registerPreviewRenderer({
    id: FALLBACK_RENDERER_ID,
    priority: PRIORITY.fallback,
    match: () => true,
    needs: () => "none",
    Component: BinaryFallback,
  });
  // Notes, spreadsheets and source: registered above the plain-text renderer's band.
  registerPreviewRenderer(markdownRenderer);
  registerPreviewRenderer(csvRenderer);
  registerPreviewRenderer(codeRenderer);
}

registerDefaultPreviewRenderers();

// preview — one registry of file renderers, shared by every surface that shows a
// file (a tab beside a chat, the Files page modal, the page a share link lands on).
// The renderers and the surface that draws a plan land here too.
export {
  FALLBACK_RENDERER_ID,
  planPreview,
  planPreviewWith,
  previewRendererById,
  registerPreviewRenderer,
  unregisterPreviewRenderer,
} from "./registry";
export type {
  PreviewContent,
  PreviewFacts,
  PreviewFileRef,
  PreviewNeed,
  PreviewPlan,
  PreviewProps,
  PreviewRenderer,
  PreviewStatus,
  PreviewText,
  PreviewViewSetting,
} from "./types";
export { PreviewSurface, type PreviewSurfaceProps } from "./PreviewSurface";
export { PreviewNotice } from "./renderers/BinaryFallback";
export { registerDefaultPreviewRenderers } from "./defaults";
export { extensionOf, isEditableText, mimeEssence, previewKindFor, type PreviewKind } from "./kind";
export { formatBytes } from "./size";

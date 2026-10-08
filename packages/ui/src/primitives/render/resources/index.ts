// resources — rendering a resource reference's preview: a code / diff / table view, a language glyph,
// a diff count, a reference chip list, a path. Shares the render domain's highlight engine + diff
// tokens. The tool-card-coupled ResourceList (rows in a ToolFrame) lives in chat/tool-cards.
export {
  ResourcePreview,
  ResourcePreviewBlock,
  LanguageIcon,
  DiffCount,
  ReferenceList,
  type ResourcePreviewProps,
  type LanguageIconProps,
  type ReferenceListProps,
} from "./ResourcePreview";
export {
  PathDisplay,
  displayPath,
  WorkspacePathsProvider,
  useDisplayPath,
  type WorkspacePathContext,
  type WorkspacePathsProviderProps,
} from "./PathDisplay";
export { renderHighlightedCode } from "./highlightCode";
export {
  parseUnifiedDiff,
  previewLanguage,
  previewLanguageLabel,
  previewTitle,
  splitPreviewLines,
  type DiffPreviewRow,
  type DiffLineKind,
} from "./previewModel";

// Direct non-chat dependencies for the canonical chat implementation. Keeping
// this internal seam off the package barrel prevents @alkera/ui from importing
// itself through every chat module.
export { Text, type TextProps } from "../primitives/display/Text";
// How a machine's facts are read by a person. Shared with the portal's receipt
// panel, so a card and the object it was saved into never date the same run
// two ways.
export { formatCount, formatDuration, formatTimestamp } from "../primitives/format";
export { Tooltip } from "../primitives/overlays/Tooltip";
// The one right-click menu: the chat wears the same panel, keyboard model and
// dismissal the rest of the product does.
export { ContextMenu, useContextMenu } from "../primitives/overlays/ContextMenu";
export type { ContextMenuItem } from "../primitives/overlays/ContextMenu";
export { UrlLink } from "../primitives/display/UrlLink";
export { useDismiss } from "../hooks/useDismiss";
export { ConnectorMark } from "../brand/ConnectorMark";
export { resolveProviderMark } from "../brand/ProviderLogo";
export { resolveModelMark } from "../brand/modelMarks";
export {
  ChatFileLink,
  ChatFileNotReady,
  ChatFilesProvider,
  ChatImage,
  ChatResultLink,
  LanguageIcon,
  chatImagePath,
  chatRelativePath,
  isChatImageName,
  langForPath,
  parseMarkdownBlocks,
  renderInline,
  renderMathHtml,
  useChatFiles,
  ChatFilesStreaming,
  CHAT_FILE_GONE_NOTE,
  CHAT_FILE_MISSING_NOTE,
  CHAT_FILE_PENDING_NOTE,
  chatFileTitle,
  type ChatFileRef,
  type ChatFilesResolver,
  tokenizeInline,
  tokenizeShell,
  tokenizeToLines,
  type CodeSeg,
  type MarkdownBlock,
  type ShellSeg,
} from "../primitives/render";
export { ReferenceList, useReferenceActions } from "../resources";
export type { ResourceReference } from "@alkera/chat-model";

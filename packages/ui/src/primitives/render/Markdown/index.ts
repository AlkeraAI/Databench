export * from "./Markdown";
// The block parser + inline renderer are public: any markdown surface renders
// its own block treatments over this one model instead of forking it.
export { parseMarkdownBlocks, type MarkdownBlock } from "./markdownBlocks";
export { renderInline, renderMathHtml } from "./markdownInline";
// The chat-folder file seam: the path rule every message follows, and the
// resolver a shell installs so images and file links inside the chat load.
export {
  CHAT_IMAGE_EXTENSIONS,
  chatImagePath,
  chatRelativePath,
  isChatImageName,
  resolveChatPath,
} from "./chatPaths";
export { chatFileLinks, type ChatFileLinkRef } from "./chatFileLinks";
export {
  CHAT_FILE_GONE_NOTE,
  CHAT_FILE_MISSING_NOTE,
  CHAT_FILE_PENDING_NOTE,
  chatFileTitle,
  ChatFileLink,
  ChatFileNotReady,
  ChatFilesProvider,
  ChatFilesStreaming,
  ChatImage,
  ChatResultLink,
  useChatFiles,
  type ChatFileRef,
  type ChatFilesResolver,
} from "./ChatFiles";

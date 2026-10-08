// render — the rich-text rendering domain: source code (CodeBlock), command output (Terminal),
// Markdown prose, and resource previews (code / diff / table). One highlight engine, one warm Alkera
// palette that adopts the VS Code editor + diff theme inside VS Code.
export { CodeBlock, langForPath, type CodeBlockProps } from "./CodeBlock";
export { Terminal, type TerminalProps } from "./Terminal";
export {
  tokenizeInline,
  tokenizeShell,
  tokenizeToLines,
  type CodeSeg,
  type ShellSeg,
  type ShellTokenKind,
} from "./highlight";
export {
  CHAT_IMAGE_EXTENSIONS,
  ChatFileLink,
  ChatFileNotReady,
  ChatFilesProvider,
  ChatImage,
  ChatResultLink,
  Markdown,
  chatImagePath,
  chatRelativePath,
  resolveChatPath,
  isChatImageName,
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
  type MarkdownBlock,
  type MarkdownProps,
} from "./Markdown";
export * from "./resources";

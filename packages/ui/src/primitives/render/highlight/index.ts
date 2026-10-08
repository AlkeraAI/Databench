// The code group's tokenizers. Prism drives the CodeBlock grammar highlighting; the shell tokenizer
// drives the Terminal prompt. Both emit React-safe classed segments, never raw HTML.
export { langForExtension, langForPath, tokenizeInline, tokenizeToLines, type CodeSeg } from "./prism";
export { tokenizeShell, type ShellSeg, type ShellTokenKind } from "./shell";

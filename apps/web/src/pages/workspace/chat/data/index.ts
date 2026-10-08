// The chat host composition's data barrel: the contract, the installed pair,
// the model, and the shape-agnostic helpers.
//
// Everything under `pages/workspace/chat/` reads its data through here, so
// neither shell's plumbing leaks into the composition.

export type {
  ApprovalVerdict,
  CancelOutcome,
  ChatAccount,
  ChatCapabilities,
  ChatDataSource,
  ChatDocumentRequest,
  ChatEmptyState,
  ChatHost,
  ChatHostMessage,
} from "./ChatDataSource";
export { APPROVAL_REACHES, APPROVAL_WITHHELD, READ_ONLY_REFUSAL } from "./ChatDataSource";
export { createBrowserChatHost } from "./browserChatHost";
export {
  chatCaps,
  chatData,
  chatHost,
  chatRuntime,
  installChatRuntime,
  releaseChatRuntime,
  resetChatRuntime,
} from "./runtime";
export type { ChatRuntime, ChatRuntimeOwner } from "./runtime";
export {
  errorText,
  transportFailure,
  isSessionNotOpen,
  refetchWhileErrored,
  refetchWhileErroredOrEmpty,
  refetchWhileNoChatDefault,
} from "./errors";
export { EXPORT_TRUNCATED_NOTICE, exportBlob } from "./exportBlob";
export { cloudChatFiles, stagedFileName, type ChatFilesPort } from "./chatFiles";
export type * from "./model";

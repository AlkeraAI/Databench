// The conversation model lives in @alkera/chat-model (React-free, fold-canonical);
// this file re-exports the names ui's chat components consume so the package's
// internal `../types` imports stay stable. New code should import the model from
// @alkera/chat-model directly.
export type {
  BlobReference,
  CommandConversationPart,
  CompactionConversationPart,
  ConversationAuthor,
  ConversationMessage,
  ConversationPart,
  ConversationPartBase,
  ConversationRole,
  ConversationTurn,
  FileEditedConversationPart,
  PermissionConversationPart,
  PlanConversationPart,
  PlanEntryView,
  QuestionConversationPart,
  ResourceKind,
  // ResourcePreview (the type) stays un-re-exported here: primitives/render exports a
  // ResourcePreview COMPONENT and the flat barrel would collide. Use ResourcePreviewData
  // (the alias) or import the type from @alkera/chat-model directly.
  ResourcePreviewData,
  ResourceReference,
  SubagentActivity,
  SubagentActivityTool,
  SubagentConversationPart,
  SystemConversationPart,
  TextConversationPart,
  ThinkingConversationPart,
  ToolConversationPart,
  ToolState,
  TurnSummaryConversationPart,
} from "@alkera/chat-model";

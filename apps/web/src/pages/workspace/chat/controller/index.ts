// The headless chat controller: every piece of chat orchestration —
// send/create, the stall watchdog, interrupt resolution, the sidecar handoff,
// permission-mode seeding + live push, catalog/preference queries — with ZERO
// JSX, so any surface can compose its own view over the same machine.
//
// Five hooks own five concerns; this root wires the edges that cross them:
// the transcript's send options need the composer's catalog and the create flow
// claims the identity's draft state. A mode switch answers no ask here: the
// machine that decides every ask decides a pending one again under the new
// mode, and its resolution reaches this surface like any other.

import { useLocation } from "react-router-dom";
import type { ChatHandoff } from "../openSurfaces";
import { useChatIdentity, type ChatIdentity } from "./useChatIdentity";
import { useChatInterrupts, type ChatInterrupts } from "./useChatInterrupts";
import { useChatNavigation, type ChatNavigation } from "./useChatNavigation";
import { useChatTranscript, type ChatTranscript } from "./useChatTranscript";
import { useComposerPrefs, type ChatComposer } from "./useComposerPrefs";

export { applyLocalInterruptState } from "./useChatInterrupts";
export {
  DEFAULT_PROMPTS,
  STALL_TURN,
  WORKSPACE_LOST_TURN,
  WORKSPACE_LOST_TURN_QUIET,
  createErrorTurn,
  stripThinkingParts,
  type StallCause,
} from "./useChatTranscript";
export {
  backTargetFromSearch,
  chatIdFromPath,
  findSubagentSource,
  stackedTarget,
  type SubagentContext,
} from "./useChatNavigation";
// Re-exported from the shared fold so the data source's activity view, the chat
// store, and the controller derive the working state from the same rule.
export { conversationAwaitsResponse } from "../data/harnessEventFold";

export interface ChatControllerOptions {
  /** The chat to open, when the shell knows it without going through the route.
   *  When omitted the route names it (`:chatId` in the portal, `:id` in the
   *  editor); a route with neither is the new-chat surface, never the newest
   *  chat in the list. */
  chatId?: string;
  /** The shell's word on the machine this chat's turns run on: true once it is
   *  KNOWN not to be serving. The stall watchdog uses it to name what actually
   *  went wrong instead of blaming a backend the reader does not own. */
  machineUnavailable?: boolean;
  /** The workspace a chat started from the empty composer is made in. */
  newChatWorkspaceId?: string;
}

export interface ChatController {
  identity: Pick<ChatIdentity, "chatId" | "title" | "currentTitle">;
  transcript: ChatTranscript;
  interrupts: Pick<
    ChatInterrupts,
    "resolvePermission" | "expirePermission" | "answerQuestion" | "rejectQuestion"
  >;
  composer: Omit<ChatComposer, "models">;
  nav: ChatNavigation;
}

export function useChatController({
  chatId: explicitChatId,
  machineUnavailable = false,
  newChatWorkspaceId,
}: ChatControllerOptions = {}): ChatController {
  const location = useLocation();
  // A first message handed over from the sidecar. Read once here; every hook
  // that needs it gets the same value.
  const handoff = location.state as ChatHandoff | null;

  const identity = useChatIdentity({ explicitChatId, handoff });
  const interrupts = useChatInterrupts(identity.chatId);
  // The raw catalog is composition wiring (submitMessage builds send options
  // from it); no surface consumes it, so it stays off the public shape.
  const { models, ...composer } = useComposerPrefs({
    chatId: identity.chatId,
    currentChat: identity.currentChat,
    handoff,
  });
  const transcript = useChatTranscript({
    chatId: identity.chatId,
    handoff,
    models,
    reasoningVisible: composer.reasoningVisible,
    interrupts,
    endCreating: identity.endCreating,
    machineUnavailable,
    newChatWorkspaceId,
  });
  const nav = useChatNavigation({
    chatId: identity.chatId,
    title: identity.title,
    chats: identity.chats,
  });

  return {
    identity: {
      chatId: identity.chatId,
      title: identity.title,
      currentTitle: identity.currentTitle,
    },
    transcript,
    interrupts: {
      resolvePermission: interrupts.resolvePermission,
      expirePermission: interrupts.expirePermission,
      answerQuestion: interrupts.answerQuestion,
      rejectQuestion: interrupts.rejectQuestion,
    },
    composer,
    nav,
  };
}

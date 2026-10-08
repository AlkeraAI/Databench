// Answering the asks a turn stops on, plus the optimistic resolutions that
// clear the composer-adjacent panels immediately — before the machine echoes
// the resolution back.
//
// The answer itself goes through the DATA SOURCE, not the shell: the extension
// settles the ask on the daemon that is holding the harness's wait, and the
// browser relays the same answer to the machine that raised it. Routing it
// through the shell's engine channel is what left the browser unable to answer
// at all — a card the reader could press whose promise rejected unhandled,
// while the turn hung on an ask nobody could release.

import type { ConversationTurn } from "@alkera/chat-model";
import { useCallback, useEffect, useRef, useState } from "react";
import { chatData } from "../data";

/** The webview's optimistic interrupt resolutions, keyed by request id. */
export interface InterruptOverlay {
  resolvedPermissions: Record<string, string>;
  /** Asks the machine is no longer holding: the answer came back 404/410, so
   *  there is nothing to answer and the card settles without one. */
  expiredPermissions: Record<string, true>;
  answeredQuestions: Record<string, string[][]>;
  /** The note a reader sent beside an answer, so the card shows it before the
   *  recorded answer comes back. */
  answeredNotes?: Record<string, string>;
  rejectedQuestions: Record<string, string | null>;
}

/** Overlay the webview's optimistic interrupt resolutions (permission/question
 *  answers) onto the folded turns so composer-adjacent panels clear immediately,
 *  before the daemon echoes the resolution back. */
export function applyLocalInterruptState(
  turns: ConversationTurn[],
  overlay: InterruptOverlay,
): ConversationTurn[] {
  if (
    Object.keys(overlay.resolvedPermissions).length === 0
    && Object.keys(overlay.expiredPermissions).length === 0
    && Object.keys(overlay.answeredQuestions).length === 0
    && Object.keys(overlay.rejectedQuestions).length === 0
  ) {
    return turns;
  }
  return turns.map((turn) => ({
    ...turn,
    parts: turn.parts.map((part) => overlaidPart(part, overlay)),
  }));
}

function overlaidPart(
  part: ConversationTurn["parts"][number],
  overlay: InterruptOverlay,
): ConversationTurn["parts"][number] {
  if (part.kind === "permission" && overlay.expiredPermissions[part.requestId]) {
    // Settled, with no option named: nobody answered it, the machine stopped
    // waiting. A selected option here would claim a decision that was not made.
    return { ...part, status: "resolved" as const };
  }
  if (part.kind === "permission" && overlay.resolvedPermissions[part.requestId]) {
    return {
      ...part,
      status: "resolved" as const,
      selectedOptionId: overlay.resolvedPermissions[part.requestId],
    };
  }
  if (part.kind === "question" && overlay.answeredQuestions[part.requestId]) {
    return {
      ...part,
      status: "answered" as const,
      answers: overlay.answeredQuestions[part.requestId],
      ...(overlay.answeredNotes?.[part.requestId] ? { note: overlay.answeredNotes[part.requestId] } : {}),
    };
  }
  if (part.kind === "question" && part.requestId in overlay.rejectedQuestions) {
    return {
      ...part,
      status: "rejected" as const,
      reason: overlay.rejectedQuestions[part.requestId],
    };
  }
  return part;
}

export interface ChatInterrupts {
  resolvedPermissions: Record<string, string>;
  expiredPermissions: Record<string, true>;
  answeredQuestions: Record<string, string[][]>;
  answeredNotes: Record<string, string>;
  rejectedQuestions: Record<string, string | null>;
  resolvePermission: (requestId: string, optionId: string) => Promise<void>;
  /** Settle an ask the machine is no longer holding. */
  expirePermission: (requestId: string) => void;
  answerQuestion: (requestId: string, answers: string[][], note?: string | null) => Promise<void>;
  rejectQuestion: (requestId: string, reason?: string | null) => Promise<void>;
}

export function useChatInterrupts(chatId: string | null): ChatInterrupts {
  const [resolvedPermissions, setResolvedPermissions] = useState<Record<string, string>>({});
  const [expiredPermissions, setExpiredPermissions] = useState<Record<string, true>>({});
  const [answeredQuestions, setAnsweredQuestions] = useState<Record<string, string[][]>>({});
  const [answeredNotes, setAnsweredNotes] = useState<Record<string, string>>({});
  const [rejectedQuestions, setRejectedQuestions] = useState<Record<string, string | null>>({});

  // Reset only on an ACTUAL chat switch, or chat A's resolutions overlay chat
  // B's turns. Comparing the previous chatId (not a mount-skip flag) is
  // idempotent under StrictMode's dev double-invoke.
  const prevChatIdRef = useRef(chatId);
  useEffect(() => {
    if (prevChatIdRef.current === chatId) return;
    prevChatIdRef.current = chatId;
    setResolvedPermissions({});
    setExpiredPermissions({});
    setAnsweredQuestions({});
    setRejectedQuestions({});
  }, [chatId]);

  const resolvePermission = useCallback(
    async (requestId: string, optionId: string): Promise<void> => {
      await chatData().resolvePermission(chatId ?? "", requestId, optionId);
      setResolvedPermissions((current) => ({ ...current, [requestId]: optionId }));
    },
    [chatId],
  );

  const expirePermission = useCallback((requestId: string): void => {
    setExpiredPermissions((current) => ({ ...current, [requestId]: true }));
  }, []);

  const answerQuestion = useCallback(
    async (requestId: string, answers: string[][], note?: string | null): Promise<void> => {
      // A note travels only when there is one; every other answer reads as before.
      if (note) await chatData().answerQuestion(chatId ?? "", requestId, answers, note);
      else await chatData().answerQuestion(chatId ?? "", requestId, answers);
      setAnsweredQuestions((current) => ({ ...current, [requestId]: answers }));
      if (note) setAnsweredNotes((current) => ({ ...current, [requestId]: note }));
      // A plan approval flips the daemon's permission mode server-side; the pill
      // follows authoritatively via the `harness.session_state_changed` push
      // (the composer-prefs subscription), so there's no optimistic label-guess
      // to maintain.
    },
    [chatId],
  );

  const rejectQuestion = useCallback(
    async (requestId: string, reason?: string | null): Promise<void> => {
      const normalizedReason = reason ?? null;
      await chatData().rejectQuestion(chatId ?? "", requestId, normalizedReason);
      setRejectedQuestions((current) => ({ ...current, [requestId]: normalizedReason }));
    },
    [chatId],
  );

  return {
    resolvedPermissions,
    expiredPermissions,
    expirePermission,
    answeredQuestions,
    answeredNotes,
    rejectedQuestions,
    resolvePermission,
    answerQuestion,
    rejectQuestion,
  };
}

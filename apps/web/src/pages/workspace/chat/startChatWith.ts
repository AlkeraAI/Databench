// "Start a new chat with <model>": the way to use a model an open chat may not
// move to (its reasoning would be lost, or its agent cannot drive that model).
//
// The new chat is the shell's empty composer, in the same workspace, with the
// model already picked and the words the reader had typed carried into the
// field, unsent. Nothing is created until they send.

import { useCallback } from "react";
import { useNavigate } from "react-router-dom";

import { NEW_CHAT_WORKSPACE_PARAM } from "../workspaces/useWorkspaceRail";
import { DRAFT_CHAT_KEY, useChatStore } from "./chatStore";
import { chatRoutes } from "./chatRoutes";

/** What the router carries to the new chat's composer. */
export interface CarriedDraftState {
  carriedDraft: { text: string; at: number };
}

/** The carried words in a route's state, or null when it carries none. */
export function carriedDraft(state: unknown): { text: string; at: number } | null {
  if (typeof state !== "object" || state === null || !("carriedDraft" in state)) return null;
  const draft = (state as CarriedDraftState).carriedDraft;
  if (typeof draft?.text !== "string" || typeof draft.at !== "number") return null;
  return draft;
}

/** Opens the empty composer on `model`. `workspaceId` is the open chat's
 *  workspace, which the new chat is aimed at; unset (a shell without
 *  workspaces), the composer goes where it always does. */
export function useStartChatWith(workspaceId?: string | null): (model: string, text: string) => void {
  const navigate = useNavigate();
  const setComposerPref = useChatStore((state) => state.setComposerPref);
  return useCallback(
    (model: string, text: string): void => {
      setComposerPref(DRAFT_CHAT_KEY, { model, effort: undefined });
      const home = chatRoutes().home;
      const to = workspaceId ? `${home}?${NEW_CHAT_WORKSPACE_PARAM}=${encodeURIComponent(workspaceId)}` : home;
      navigate(to, {
        state: { carriedDraft: { text, at: Date.now() } } satisfies CarriedDraftState,
      });
    },
    [navigate, setComposerPref, workspaceId],
  );
}

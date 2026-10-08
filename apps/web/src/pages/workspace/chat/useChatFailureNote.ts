// What a failed turn offers the reader, in the shell they are reading it in.
//
// The transcript already says WHY a turn failed — the fold classifies the
// raw sentence into a cause (see `data/harnessEventFold.ts`). What it cannot
// know is what this particular reader can do about that cause, and that
// answer does not belong in a pure fold over wire events.
//
// So the cause is resolved HERE into at most one line of guidance and at most
// one control. A cause with nothing to offer this reader resolves to null and
// the card states the cause alone. Causes the open chat does not own (money
// refusals, for one) are answered by an arm an extension registers on
// CHAT_FAILURE_NOTES; with none registered they state the cause alone.
//
// A refusal may bring its own facts off the wire, such as whether the gateway
// decided THIS caller may open the page that fixes it. That verdict counts in
// every shell; the portal's identity query only where it was allowed to run.

import { useQuery } from "@tanstack/react-query";
import { useCallback } from "react";

import type { NoticeAction } from "@alkera/ui";

import { api, request } from "../../../api/client";
import { CHAT_FAILURE_NOTES } from "../../../app/extensions/portal";
import { keys } from "../../../api/keys";

/** The guidance a cause carries for this reader: a quieter second line, a
 *  control, or both. */
export interface ChatFailureNote {
  body?: string;
  action?: NoticeAction | null;
}

/** The facts a failed turn's part carries alongside its cause. */
export interface FailureFacts {
  resetsAt?: string | null;
  manageUrl?: string | null;
}

export const RETRY = "Retry";

/** Build the resolver the transcript asks per failed turn.
 *
 *  `enabled` is false in every shell that is not the browser portal: the
 *  query below is a portal endpoint, and the editor's webview must not fire
 *  it across its bridge merely because a turn failed. Disabled, the resolver
 *  still answers — with the cause's shell-independent guidance — so the
 *  editor's reader is never worse off than before.
 *
 *  A new cause is served by adding an arm below; nothing else changes. */
export function useChatFailureNote({
  enabled,
  onRetry,
}: {
  enabled: boolean;
  onRetry?: (() => void) | undefined;
}): (cause: string, facts?: FailureFacts) => ChatFailureNote | null {
  const identity = useQuery({
    queryKey: keys.dashboard.identity,
    queryFn: () => request(api.GET("/api/v1/dashboard"), "could not load your dashboard"),
    enabled,
  });

  const isOrgAdmin = Boolean(
    (identity.data as { is_org_admin?: boolean } | undefined)?.is_org_admin,
  );

  return useCallback(
    (cause: string, facts?: FailureFacts): ChatFailureNote | null => {
      // The admin gets the dial; everyone else gets the person who holds it.
      // The wire's own verdict on who may act counts in every shell, the
      // portal's identity query only where it was allowed to run.
      const canManage = Boolean(facts?.manageUrl) || (enabled && isOrgAdmin);
      const arm = CHAT_FAILURE_NOTES.items().find((entry) => entry.causes.includes(cause));
      if (arm) return arm.resolve(cause, { facts, canManage, portal: enabled });
      switch (cause) {
        case "provider_unavailable":
          // Retrying is the whole remedy, and it resends the message that was
          // already accepted — no second bubble for the same words.
          return onRetry ? { action: { label: RETRY, onClick: onRetry } } : null;
        case "context_too_long":
          return { body: "Start a new chat, or run /compact to shorten this one." };
        case "model_unavailable":
          // The provider-keys page exists only on a self-hosted install, and
          // is hidden everywhere else — naming it would dead-end most admins.
          // The composer is the one move every reader actually has.
          return { body: "Pick another model from the composer." };
        case "workspace_stopped":
        case "agent_stopped":
          // The banner above owns the diagnosis and says what happens next, so
          // the card adds no second line of the same instruction. What it does
          // add is the move: this turn was lost mid-answer, and sending the
          // words again is the whole remedy. The control is here only while the
          // shell can actually take a turn — the caller withholds `onRetry`
          // otherwise — so it is never a button beside a composer that is
          // refusing input for the same reason.
          return onRetry ? { action: { label: RETRY, onClick: onRetry } } : null;
        default:
          return null;
      }
    },
    [enabled, isOrgAdmin, onRetry],
  );
}

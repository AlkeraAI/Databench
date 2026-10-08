// The picks on a chat that does not exist yet, kept across a reload.
//
// The empty composer's model, effort and stance live in the chat store under
// the draft key, which a reload empties: a reader who chose Plan, High and a
// smaller model came back to the defaults with nothing saying the choice had
// gone. So the draft's picks are mirrored into this browser's storage, per
// signed-in account and org, until the chat they were made for is created — that
// create claims them into the new chat and the memory is dropped with the
// draft, so a pick never outlives the chat it was for and never overrides the
// reader's saved defaults on a later one.
//
// A stance that lets the agent do more than the default does is not
// remembered: a new chat must never open, after a reload, in a stance the
// reader was not looking at when they chose it. The store is a convenience
// (the shared guarded one); a browser that refuses it keeps today's behaviour.

import { useEffect } from "react";

import { accountKey, safeLocalStorage } from "@alkera/ui/storage";

import { chatAccountScope, type AccountScope } from "@/lib/accountScope";

import { DRAFT_CHAT_KEY, useChatStore, type ComposerPrefs } from "./chatStore";
import { useChatAccount } from "./useChatAccount";


/** The stances a remembered pick may restore: the ones whose worst case is
 *  still an ask, or no write at all. */
const REMEMBERED_MODES: ReadonlySet<string> = new Set(["read_only", "plan", "default"]);

/** Per person and org, so a pick made in one org is never restored in another.
 *  A key from before keys named the org is ignored, not migrated: these are
 *  conveniences, and the composer opens at the reader's defaults once. */
const keyFor = (scope: AccountScope): string => accountKey(scope.userId, scope.orgId, "chat.draftPicks");

/** The picks worth keeping, or null when there are none. Only strings survive:
 *  a stored value from an older build, or one edited by hand, is dropped
 *  rather than handed to the composer. */
function keepable(prefs: unknown): ComposerPrefs | null {
  if (typeof prefs !== "object" || prefs === null) return null;
  const raw = prefs as Record<string, unknown>;
  const kept: ComposerPrefs = {};
  if (typeof raw.model === "string" && raw.model) kept.model = raw.model;
  if (typeof raw.effort === "string" && raw.effort) kept.effort = raw.effort;
  if (typeof raw.mode === "string" && REMEMBERED_MODES.has(raw.mode)) kept.mode = raw.mode;
  return Object.keys(kept).length > 0 ? kept : null;
}

/** The picks this account left on its empty composer, or null. */
export function readDraftPicks(scope: AccountScope | null | undefined): ComposerPrefs | null {
  if (!scope) return null;
  const stored = safeLocalStorage().get(keyFor(scope));
  if (!stored) return null;
  try {
    return keepable(JSON.parse(stored));
  } catch {
    return null;
  }
}

function writeDraftPicks(scope: AccountScope, prefs: ComposerPrefs | undefined): void {
  const kept = keepable(prefs);
  if (kept === null) safeLocalStorage().remove(keyFor(scope));
  else safeLocalStorage().set(keyFor(scope), JSON.stringify(kept));
}

/** Keep the draft's picks for the signed-in account across a reload: restore
 *  them into an empty draft on mount, and follow every change after. Mounted by
 *  each surface that shows the empty composer. */
export function useRememberedDraftPicks(): void {
  // The browser names the person and the org; the editor names only the email,
  // which keys its picks the way it always has, with no org.
  const scope = chatAccountScope(useChatAccount());
  const userId = scope?.userId ?? null;
  const orgId = scope?.orgId ?? "";
  useEffect(() => {
    if (!userId) return undefined;
    const account: AccountScope = { userId, orgId };
    const store = useChatStore.getState();
    if (store.composerPrefs[DRAFT_CHAT_KEY] === undefined) {
      const restored = readDraftPicks(account);
      if (restored) store.setComposerPref(DRAFT_CHAT_KEY, restored);
    }
    return useChatStore.subscribe((state, prior) => {
      const next = state.composerPrefs[DRAFT_CHAT_KEY];
      if (next === prior.composerPrefs[DRAFT_CHAT_KEY]) return;
      writeDraftPicks(account, next);
    });
  }, [userId, orgId]);
}

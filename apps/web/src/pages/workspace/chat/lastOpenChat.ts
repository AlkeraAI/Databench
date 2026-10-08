// Which chat this browser was last reading, per signed-in account and org.
//
// Chat is a place, not a form: leaving it for Files or Connections and coming
// back through the nav should land on the conversation that was open, the way
// switching tabs in an editor does. The nav leaf can only name one path
// (`/chat`), so the chat it resolves to is remembered here.
//
// Per account and org: the key carries the user id and the org, so a second
// person signing in on the same laptop never lands in the first one's chat, a
// person who switches orgs is never offered a chat from the org they left, and
// signing out leaves nothing another reader can follow. The store is a
// convenience — it is the shared guarded one (`@alkera/ui/storage`), and a
// browser that refuses it (a private window, cleared site data, an embedded
// webview) simply opens the chat surface with nothing selected.

import { accountKey, safeLocalStorage } from "@alkera/ui/storage";

import { isAccountKeyOf, type AccountScope } from "@/lib/accountScope";

const FAMILY = "chat.last";

// A key from before keys named the org is ignored, not migrated: this is a
// convenience, and the worst a lost one costs is one click.
const keyFor = (scope: AccountScope): string => accountKey(scope.userId, scope.orgId, FAMILY);

/** The chat this account last had open, or `null` — no stored value, an
 *  unreadable store, or an account this browser has never opened a chat for. */
export function readLastChat(scope: AccountScope | null | undefined): string | null {
  if (!scope) return null;
  const stored = safeLocalStorage().get(keyFor(scope));
  return stored && stored.trim() !== "" ? stored : null;
}

export function rememberLastChat(scope: AccountScope | null | undefined, chatId: string): void {
  if (!scope || !chatId) return;
  // A store that will not take the write costs this reader the shortcut and
  // nothing else.
  safeLocalStorage().set(keyFor(scope), chatId);
}

/** Forget this account's remembered chat, if it is the one named. Naming it
 *  matters: a chat opened, refused and then closed must not wipe the memory of
 *  the chat the reader actually has. */
export function forgetLastChat(scope: AccountScope | null | undefined, chatId: string): void {
  if (!scope) return;
  const storage = safeLocalStorage();
  if (storage.get(keyFor(scope)) === chatId) storage.remove(keyFor(scope));
}

/** Forget a chat that no longer exists, for whoever was remembering it.
 *
 *  Deleting a chat is the one case where the id has to go without knowing whose
 *  memory holds it: the delete takes the reader back to the chat home, and a
 *  home that then resolved to the chat they just deleted would put them back in
 *  front of a dead transcript. */
export function forgetDeletedChat(chatId: string): void {
  if (!chatId) return;
  const storage = safeLocalStorage();
  const stale = storage
    .keys()
    .filter((key) => isAccountKeyOf(key, FAMILY) && storage.get(key) === chatId);
  for (const key of stale) storage.remove(key);
}

// Whose browser memory a value belongs to: one person in one org.
//
// A login can belong to several orgs and switch between them in the same
// browser, so anything this browser remembers about org data (the chat last
// open, a queued message, an unfinished upload) is filed under the person AND
// the org it was made in. Every such key is built by `accountKey` from
// `@alkera/ui/storage`; this module only says where the two halves come from.

import { accountKey } from "@alkera/ui/storage";

/** The person and the org a remembered value is filed under. `orgId` is "" in a
 *  shell that does not know the org (the editor), which keys apart from every
 *  real org. */
export interface AccountScope {
  readonly userId: string;
  readonly orgId: string;
}

/** The portal's scope, from the signed-in user (`/auth/me`): `org_team_id` is
 *  the org this session is acting in. Null until the user is known. */
export function userScope(
  user: { id: string; org_team_id?: string | null } | null | undefined,
): AccountScope | null {
  if (!user?.id) return null;
  return { userId: user.id, orgId: user.org_team_id ?? "" };
}

/** The scope a chat shell's account line names. The browser fills `userId` and
 *  `orgId`; a shell that knows only the email (the editor) is keyed by the email
 *  with no org, which is what it was keyed by before orgs were named. Null when
 *  the shell knows nobody yet. */
export function chatAccountScope(
  account: { email: string | null; userId?: string | null; orgId?: string | null } | null | undefined,
): AccountScope | null {
  const userId = account?.userId || account?.email;
  if (!userId) return null;
  return { userId, orgId: account?.orgId ?? "" };
}

/** Whether `key` is one of `name`'s keys, for any person in any org. For the
 *  sweeps that must find a value without knowing whose it is (a deleted chat, a
 *  sign-out). The family's head is read off `accountKey` itself, so the sweep
 *  follows the key shape rather than spelling it a second time. */
export function isAccountKeyOf(key: string, name: string): boolean {
  const probe = accountKey("\u0000", "\u0000", name);
  return key.startsWith(probe.slice(0, probe.indexOf("\u0000")));
}

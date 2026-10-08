// A person's colour, the same on every surface that draws them.

/** Who a hue is derived from: a login address, else a user id. */
export interface HueIdentity {
  userId: string;
  email: string;
}

/** A hue that is this person's everywhere (their face, their caret in the
 *  composer and in a file, on every reader's screen), derived from who they
 *  are and nothing else, so two windows agree without exchanging a palette.
 *
 *  The key is their login address, lower-cased and trimmed: that is the person,
 *  where a user id is only the person-in-this-deployment. It falls back to the
 *  user id where the roster carries no address (an older server, a peer whose
 *  user row is gone). Never the peer id and never a position in the roster:
 *  both change per tab and per join order, which would repaint a colleague on
 *  every refresh. */
export function hueOf(who: HueIdentity | string): number {
  const identity = typeof who === "string" ? { userId: who, email: "" } : who;
  const key = identity.email.trim().toLowerCase() || identity.userId;
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  return hash % 360;
}

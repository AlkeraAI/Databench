// The product's password + display-name policies live here; the generic field validators are
// re-exported from @alkera/ui (their one home), so existing callers keep importing them from
// "../lib/validation".
//
// Both policies MIRROR a server-side rule — they never replace it. The server is the enforcement
// (`alkera_core.validation`, `backend.auth.password_policy`); these exist so the person typing gets
// the answer before a round-trip, and so the form can say which box is wrong. When the two
// disagree, the server wins and its message is what the page renders.

import { minLength, type Validator } from "@alkera/ui";

export { required, isEmail, minLength, firstError, matches, type Validator } from "@alkera/ui";

/** The one place the password policy length lives — shared by the validator, the field's helper
 *  copy, and any test, so the three never drift. Mirrors `auth_password_min_length`. */
export const PASSWORD_MIN = 12;

/** The account password rule, message and length sourced from PASSWORD_MIN.
 *
 *  Length only. The server's policy also refuses a password derived from the account's own email
 *  or name and a handful of common words — checks that need the identity and a word list, and that
 *  are better delivered as the server's own sentence than guessed at here. */
export const isPassword = (): Validator => minLength(PASSWORD_MIN, `Use at least ${PASSWORD_MIN} characters`);

/** Longest org / team / person name the server accepts. Mirrors `MAX_DISPLAY_NAME_LENGTH`. */
export const DISPLAY_NAME_MAX = 100;

// Kept deliberately in step with alkera_core/validation/display_name.py. A bare dotted token
// (`Acme.io`) is allowed there and here — banning it would reject a large share of real company
// names, and it is a knowingly-accepted residual, not an oversight.
const SCHEME_RE = /[a-z][a-z0-9+.-]*:\/\//i;
const WWW_RE = /(?:^|[\s([<])www\./i;
const HOST_PATH_RE = /[a-z0-9-]+(?:\.[a-z0-9-]+)+\//i;
const EMAIL_LIKE_RE = /[^\s@]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+/i;

/**
 * An org / team / person name.
 *
 * These names are embedded in outbound invitation email — prose and subject line — where a mail
 * client turns URL-shaped text into a live link on a message carrying our own sending domain. So a
 * link in a name is not a formatting preference; it is a phishing primitive, and the message says
 * so rather than just "invalid".
 */
export const isDisplayName =
  (): Validator =>
  (value) => {
    const name = value.trim();
    if (!name) return null; // emptiness is `required()`'s job, not this one's
    if (/[<>]/.test(name)) return "Names can't contain < or >";
    if (SCHEME_RE.test(name) || WWW_RE.test(name) || HOST_PATH_RE.test(name)) {
      return "Names can't contain a web address, because they appear in invitation emails";
    }
    if (EMAIL_LIKE_RE.test(name)) {
      return "Names can't contain an email address, because they appear in invitation emails";
    }
    if (name.length > DISPLAY_NAME_MAX) return `Use at most ${DISPLAY_NAME_MAX} characters`;
    return null;
  };

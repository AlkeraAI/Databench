"""Credential-strength policy -- the pure half.

Everything here is a rule about the password's own shape or its relationship to
the identity choosing it. No wordlist, no data file: ``alkera_core`` is compiled
into the shipped ``alkera`` binary, and a vendored list would ride along for no
reason (the same argument that put the free-email-domain snapshot in
``backend.auth``). The list-backed half lives in ``backend.auth.password_policy``,
which composes this with a common-password check and is what the routes call.

The rule that matters most is the identity check. A password equal to the
account's own email address survives any length or character-class requirement
you care to write, and it is the first thing a credential-stuffing run tries --
the address is the one secret an attacker targeting an account already has.
"""

from __future__ import annotations

from typing import Any

#: NIST SP 800-63B puts the floor at 8 and tells you to prefer length over
#: composition rules. 12 is the modern working default: it is where offline
#: cracking of a non-dictionary passphrase stops being cheap, and it is short
#: enough that a person can still type it.
DEFAULT_MIN_PASSWORD_LENGTH = 12

#: What a request model publishes for a password a person chooses. The policy
#: (``settings.auth_password_min_length``, this default unless a deployment
#: raises it) is what refuses a short one, with a ``400 weak_password`` that says
#: why; the model keeps its own looser floor so that answer stays the policy's,
#: and states the policy's length in the schema so a client built from it does
#: not offer a password the server will refuse.
USER_PASSWORD_SCHEMA: dict[str, Any] = {"minLength": DEFAULT_MIN_PASSWORD_LENGTH}

#: Below this a shared substring says nothing -- "an" appearing in both an
#: address and a password is coincidence, not reuse.
_MIN_SIMILARITY_FRAGMENT = 4
#: Names are shorter than local parts, so they get a lower bar ("kim", "wu").
_MIN_NAME_FRAGMENT = 3
#: How much of the password a matched identity fragment must ACCOUNT FOR before
#: it counts as "derived from". Bare containment is too blunt: a user named Ann
#: would be refused ``planned-vacation-2026``, which is a perfectly good password
#: that has nothing to do with her name. Requiring the fragment to be a real
#: share of the password keeps ``robinlee-secret`` refused and lets the
#: coincidence through. One threshold for name, local part, and domain alike --
#: NIST names all three in the same breath ("the name of the service, the
#: username, and derivatives thereof").
_IDENTITY_SHARE_OF_PASSWORD = 0.25


class PasswordPolicyError(ValueError):
    """A password fails the policy. The message is user-facing: it must say what
    to change without ever echoing the password back."""


def _fold(value: str) -> str:
    """Case-fold and drop separators, so ``Jordan.River`` and ``jordanriver``
    compare equal. An attacker guessing from an address does not respect
    punctuation, so neither does the check.

    ``isalnum`` rather than an ASCII character class: a passphrase written in a
    non-Latin script is a perfectly good password, and an ASCII-only fold would
    reduce it to the empty string and refuse it for "containing no letters".
    """
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def _sequential(folded: str) -> bool:
    """Whether the whole string is one run over the keyboard/alphabet/digit
    orders (``12345678``, ``qwertyuiop``, ``fedcba``).

    Each order is doubled before the search so a run that wraps past the end is
    still caught: ``123456789012`` is 12 characters and clears every length
    rule, but it is the same keystroke pattern as ``12345678``.
    """
    orders = ("abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiopasdfghjklzxcvbnm")
    for order in orders:
        for candidate in (order * 2, order[::-1] * 2):
            if folded in candidate:
                return True
    return False


def _dominates(fragment: str, password: str, share: float) -> bool:
    """Whether ``fragment`` appears in ``password`` AND accounts for at least
    ``share`` of it -- the test that separates "derived from your identity" from
    "happens to share three letters"."""
    return fragment in password and len(fragment) / len(password) >= share


def _repeating_unit(folded: str) -> bool:
    """Whether the string is one short block typed over and over
    (``abcabcabcabc``). Length alone would pass it; entropy-wise it is the
    block, not the string."""
    for size in range(1, len(folded) // 2 + 1):
        if len(folded) % size == 0 and folded == folded[:size] * (len(folded) // size):
            return True
    return False


def check_password_shape(
    password: str,
    *,
    email: str | None = None,
    names: tuple[str, ...] = (),
    min_length: int = DEFAULT_MIN_PASSWORD_LENGTH,
) -> None:
    """Raise ``PasswordPolicyError`` if ``password`` is too short, too trivial,
    or derived from the identity it protects. Returns None when acceptable.

    ``email`` and ``names`` are the identity the password is being set FOR --
    pass them wherever they are known (signup, profile edit, admin create). A
    reset link carries the account, so its route can pass them too; only a
    context with genuinely no identity (a bootstrap script) omits them.
    """
    if len(password) < min_length:
        raise PasswordPolicyError(f"Password must be at least {min_length} characters long.")

    folded = _fold(password)
    if not folded:
        raise PasswordPolicyError("Password must contain letters or digits.")
    if len(set(folded)) == 1:
        raise PasswordPolicyError("Password cannot be a single repeated character.")
    if _sequential(folded):
        raise PasswordPolicyError("Password cannot be a simple keyboard or alphabet sequence.")
    if _repeating_unit(folded):
        raise PasswordPolicyError("Password cannot be a short pattern repeated over and over.")

    if email:
        local_part, _, domain = email.partition("@")
        for fragment, what in (
            (local_part, "email address"),
            (domain.split(".")[0], "email domain"),
        ):
            folded_fragment = _fold(fragment)
            if len(folded_fragment) < _MIN_SIMILARITY_FRAGMENT:
                continue
            # Either direction disqualifies: the address swallowing the password
            # (it IS the address, or a slice of it), or the address making up
            # most of the password.
            if folded in folded_fragment or _dominates(
                folded_fragment, folded, _IDENTITY_SHARE_OF_PASSWORD
            ):
                raise PasswordPolicyError(f"Password cannot be based on your {what}.")

    for name in names:
        folded_name = _fold(name)
        if len(folded_name) < _MIN_NAME_FRAGMENT:
            continue
        if _dominates(folded_name, folded, _IDENTITY_SHARE_OF_PASSWORD):
            raise PasswordPolicyError("Password cannot contain your name.")


__all__ = [
    "DEFAULT_MIN_PASSWORD_LENGTH",
    "USER_PASSWORD_SCHEMA",
    "PasswordPolicyError",
    "check_password_shape",
]

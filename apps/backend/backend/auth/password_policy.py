"""The password policy the routes enforce.

Composes the pure shape/identity rules from ``alkera_core.validation.password``
with a common-password check. The word list lives HERE rather than in
``api-core`` for the same reason the free-email-domain snapshot does: api-core is
Nuitka-compiled into the shipped ``alkera`` binary, and a credential wordlist has
no business riding along -- the backend is a server that is never compiled.

Applied at the ROUTE layer, never in the service layer. Services are also driven
by operator tooling (``make seed``, ``bootstrap-admin``), which deliberately sets
a weak local-dev credential; the policy governs what a *user* may choose, and a
route is exactly where user input arrives.
"""

from __future__ import annotations

import re

from alkera_core.config import settings
from alkera_core.validation.password import PasswordPolicyError, check_password_shape
from fastapi import HTTPException, status

#: Base words that dominate every leaked-credential corpus. Matched after the
#: password is folded and stripped of the year/punctuation suffix people append,
#: so ``Password123!`` and ``passw0rd2024`` both land on ``password``. Kept small
#: and inline on purpose -- length (12+) already does the heavy lifting, and this
#: only has to refuse the handful of choices that clear it by padding a classic.
_COMMON_BASE_WORDS: frozenset[str] = frozenset(
    {
        "123456",
        "1234567890",
        "abc123",
        "access",
        "admin",
        "administrator",
        "alkera",
        "anmeldung",
        "azerty",
        "baseball",
        "batman",
        "changeme",
        "charlie",
        "cheese",
        "computer",
        "cookie",
        "corporate",
        "dragon",
        "dummy",
        "family",
        "flower",
        "football",
        "freedom",
        "friend",
        "ginger",
        "google",
        "hello",
        "hockey",
        "iloveyou",
        "internet",
        "jennifer",
        "jessica",
        "jordan",
        "letmein",
        "liverpool",
        "login",
        "loveme",
        "manager",
        "market",
        "master",
        "michael",
        "michelle",
        "monkey",
        "mustang",
        "ninja",
        "office",
        "passer",
        "passwd",
        "password",
        "passwort",
        "phoenix",
        "please",
        "princess",
        "purple",
        "qwerty",
        "qwertyuiop",
        "rainbow",
        "ranger",
        "sample",
        "samsung",
        "scooter",
        "secret",
        "secure",
        "shadow",
        "silver",
        "soccer",
        "solution",
        "spring",
        "starwars",
        "summer",
        "sunshine",
        "superman",
        "support",
        "system",
        "temporary",
        "test",
        "testing",
        "thomas",
        "tigger",
        "trustno",
        "user",
        "username",
        "welcome",
        "whatever",
        "william",
        "winter",
        "yellow",
        "zaq12wsx",
    }
)

#: Strip the suffix people add to reach a length rule -- digits, punctuation,
#: and the exclamation mark that turns ``password`` into ``Password123!``.
_PADDING = re.compile(r"[^a-z]+$")
#: ...and the leading padding, for ``123password``.
_LEADING_PADDING = re.compile(r"^[^a-z]+")


def _base_word(password: str) -> str:
    """The password's alphabetic core: case-folded, with leading/trailing
    non-letters removed. ``Summer2024!`` -> ``summer``."""
    folded = password.casefold()
    return _LEADING_PADDING.sub("", _PADDING.sub("", folded))


def check_password(
    password: str,
    *,
    email: str | None = None,
    names: tuple[str, ...] = (),
) -> None:
    """Raise ``PasswordPolicyError`` if ``password`` is unacceptable.

    ``email`` / ``names`` are the identity the credential protects; pass them
    whenever the caller knows them so "your password is your email address" is
    caught (the exact report that prompted this policy).
    """
    check_password_shape(
        password,
        email=email,
        names=names,
        min_length=settings.auth_password_min_length,
    )
    base = _base_word(password)
    if base and (base in _COMMON_BASE_WORDS or password.casefold() in _COMMON_BASE_WORDS):
        raise PasswordPolicyError(
            "Password is too easy to guess. Choose something that isn't a common word."
        )


def enforce_password(
    password: str | None,
    *,
    email: str | None = None,
    names: tuple[str, ...] = (),
) -> None:
    """Route-layer wrapper: turn a policy failure into the 400 the SPA renders.

    A ``None`` password is the "no local credential" case (an OAuth/SSO account,
    or a PATCH that isn't touching the password) and is not this policy's
    business -- it passes through untouched.

    The structured detail carries the stable ``weak_password`` code so a client
    can distinguish it from every other 400 on the same route, matching the
    convention the captcha and work-email gates already use.
    """
    if password is None:
        return
    try:
        check_password(password, email=email, names=names)
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "weak_password", "message": str(exc)},
        ) from exc


__all__ = ["PasswordPolicyError", "check_password", "enforce_password"]

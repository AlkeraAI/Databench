"""The one seam a probe verdict passes through before it is stored or shown.

A connector's sentence is written by the service on the other end of the wire,
and some of them quote back the credential they just refused — Tinybird answers a
bad token with ``Invalid token b'p.…':``. That sentence is persisted on the
verification record and rendered in the dialog, so a person who pastes a real
token with one wrong character puts that token in a durable, readable row.

Nothing here decides a verdict. It only guarantees that whatever text reaches a
``ProbeResult.detail`` no longer contains a secret the request carried — whole,
or as the beginning of one a length cut ran through.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote

#: What one removed secret reads as.
REDACTED = "[redacted]"

#: From this length up, a run of characters spelling the secret IS the secret
#: wherever it sits, so it is substituted out anywhere in the sentence. Below the
#: floor the letters alone prove nothing — a two-character password matches inside
#: a hostname, a driver name, an ordinary word — so a shorter secret is only
#: substituted where it stands on its own, with neither neighbour alphanumeric.
#: Giving up the whole verdict on an incidental match answered "why will my host
#: not resolve?" by saying nothing at all.
_MIN_SUBSTITUTABLE = 4

#: What is said instead when the secret was the only thing the sentence carried,
#: so substituting it leaves nothing to read.
_UNPRINTABLE = "The service's reply quoted the credential, so it cannot be shown."

#: A driver's sentence is cut to a length that fits the cell it is rendered in,
#: and the cut can land in the middle of a quoted credential. Substitution is
#: exact, so what is left of that credential matches no form and would survive.
#: From this many characters up, a tail that spells the beginning of a secret is
#: that secret's beginning rather than prose that happens to start the same way.
_MIN_TRUNCATED_PREFIX = 8

#: What a length cut leaves in place of the characters it dropped.
_ELLIPSIS = "…"


def _variants(secret: str) -> list[str]:
    """Every spelling of one secret that a provider's text could carry back.

    The raw value covers Python's own ``repr`` forms (``b'tok'`` contains ``tok``);
    the percent-encoded form covers a driver that echoes the DSN it was handed.
    """
    forms = {secret, quote(secret, safe="")}
    return sorted((form for form in forms if form), key=len, reverse=True)


def _substituted_where_it_stands_alone(text: str, form: str) -> str:
    """``text`` with every standalone occurrence of a too-short ``form`` removed.

    Standalone means neither neighbour is alphanumeric, so the value a driver
    quoted back (``rejected the password zq``) goes while the same letters buried
    in a word the verdict is made of (``zq`` inside a hostname) stay. An exact
    quote is what a provider echoing a credential produces, so the fail-closed
    case is the one this still catches.
    """
    pattern = rf"(?<![0-9A-Za-z]){re.escape(form)}(?![0-9A-Za-z])"
    return re.sub(pattern, lambda _match: REDACTED, text)


def _without_truncated_secret(text: str, forms: list[str]) -> str:
    """``text`` with a secret that a length cut ran through removed from its tail.

    Every cut in this pipeline drops the END of the sentence, so a secret the cut
    landed inside survives as the last thing in the text — a prefix of the real
    value, which the exact substitution above matched against nothing. A prefix of
    a token is still the token's beginning, and it is persisted on the verification
    row and shown to every member of the team, so it goes too.

    Longest form first and longest surviving prefix first, so what is removed is
    the whole fragment rather than a recognizable start of it.
    """
    head, mark = (text[:-1], _ELLIPSIS) if text.endswith(_ELLIPSIS) else (text, "")
    for form in forms:
        # A tail matching the WHOLE form was already substituted out above.
        for size in range(min(len(head), len(form) - 1), _MIN_TRUNCATED_PREFIX - 1, -1):
            if head.endswith(form[:size]):
                return head[:-size] + REDACTED + mark
    return text


def redact_secrets(text: str, secrets: Iterable[str | None]) -> str:
    """``text`` with every secret it carried replaced, or refused outright.

    Longest form first, so a secret that contains another is not partly rewritten
    into something still recognizable. Every secret is removed in place: the
    sentence around it is the only thing that tells an admin what to fix, and it
    is given up only when the secret was the whole of it.
    """
    if not text:
        return text
    forms: list[str] = []
    short: list[str] = []
    for secret in secrets:
        value = (secret or "").strip()
        if not value:
            continue
        (forms if len(value) >= _MIN_SUBSTITUTABLE else short).extend(_variants(value))
    ordered = sorted(set(forms), key=len, reverse=True)
    cleaned = text
    for form in ordered:
        cleaned = cleaned.replace(form, REDACTED)
    # Every short form is shorter than every substitutable one, so running them
    # second keeps the longest-first rule across the whole set.
    for form in sorted(set(short), key=len, reverse=True):
        cleaned = _substituted_where_it_stands_alone(cleaned, form)
    cleaned = _without_truncated_secret(cleaned, ordered)
    if REDACTED in cleaned and not any(char.isalnum() for char in _remainder(cleaned)):
        return _UNPRINTABLE
    return cleaned


def _remainder(cleaned: str) -> str:
    """What a scrubbed sentence still says once the removals are taken out."""
    return cleaned.replace(REDACTED, "").replace(_ELLIPSIS, "")


__all__ = ["REDACTED", "redact_secrets"]

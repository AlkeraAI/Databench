"""Minting + hashing of platform machine credentials.

A machine credential is the bearer secret a chat box boots with: minted by a
platform admin in the console, put in the box's secret, presented on every
register and heartbeat. It stands for the BOX, not for any person — a pool box
serves every org's chats and belongs to none of them — so it resolves to a
service principal rather than a user. Like the proxy and personal access
tokens only its keyed HMAC digest is stored; the raw secret is shown once at
mint. The ``alk_machine_`` prefix makes a leaked one greppable and lets the
authenticator route it before any database work.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping

from alkera_core.auth.token_hash import hash_lookup_token
from alkera_core.machine_refusals import (
    FATAL_MACHINE_REFUSALS,
    MACHINE_CREDENTIAL_REFUSED,
    MACHINE_CREDENTIAL_REQUIRED,
)
from alkera_core.token_prefixes import MACHINE_TOKEN_PREFIX

#: A PUBLIC, greppable marker (the entropy follows it), not itself a credential.


#: The marker an org-bound worker credential starts with (a signed token
#: follows it). It extends the machine marker, so every layer that routes a
#: machine credential before any database work (the rate limiter, the
#: browser-session refusal, the product gate) routes this one the same way;
#: the ``.`` cannot occur in a raw machine secret (url-safe base64), so the
#: two never collide.
MACHINE_WORKER_TOKEN_PREFIX = MACHINE_TOKEN_PREFIX + "org."


def mint_machine_token() -> tuple[str, str]:
    """Return ``(raw_secret, token_hash)``. Persist only the hash."""
    raw = MACHINE_TOKEN_PREFIX + secrets.token_urlsafe(48)
    return raw, hash_lookup_token(raw)


def looks_like_machine_token(raw: str) -> bool:
    """A machine credential or an org-bound worker credential it minted."""
    return raw.startswith(MACHINE_TOKEN_PREFIX)


def looks_like_machine_worker_token(raw: str) -> bool:
    return raw.startswith(MACHINE_WORKER_TOKEN_PREFIX)


#: The header a box presents its credential in when it claims its machine.
#: The box still signs in as its box user for everything it publishes (the
#: chat transcript, the folder leases) — the credential is the platform's
#: word on WHAT the box is and WHOM it serves, carried beside that session.
#: Built here and parsed here, never spelled anywhere else.
MACHINE_CREDENTIAL_HEADER = "X-Alkera-Machine-Credential"


def machine_credential_headers(raw: str) -> dict[str, str]:
    return {MACHINE_CREDENTIAL_HEADER: raw}


def parse_machine_credential(headers: Mapping[str, str]) -> str | None:
    """The credential a request carries, or ``None``. A value that does not
    look like a machine credential reads as none: a stray header is not a
    credential."""
    raw = headers.get(MACHINE_CREDENTIAL_HEADER) or headers.get(MACHINE_CREDENTIAL_HEADER.lower())
    if not raw or not looks_like_machine_token(raw.strip()):
        return None
    return raw.strip()


__all__ = [
    "FATAL_MACHINE_REFUSALS",
    "MACHINE_CREDENTIAL_HEADER",
    "MACHINE_CREDENTIAL_REFUSED",
    "MACHINE_CREDENTIAL_REQUIRED",
    "MACHINE_TOKEN_PREFIX",
    "MACHINE_WORKER_TOKEN_PREFIX",
    "looks_like_machine_token",
    "looks_like_machine_worker_token",
    "machine_credential_headers",
    "mint_machine_token",
    "parse_machine_credential",
]

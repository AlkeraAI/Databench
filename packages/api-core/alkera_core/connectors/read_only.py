"""What keeps a connector from writing, and what is known about its credential.

The read-only badge is the sentence somebody reads before handing us a production
database, so it may only say what the code does. Two facts are different in kind and must
not be rendered as one:

* **the mechanism** — what is true of every connection to this engine, whatever credential
  it carries (Alkera dials one SELECT-only endpoint; a write statement is refused by the
  classifier before it is sent);
* **what the credential itself could do** — which depends on what the source lets us read
  back, and is a RECOMMENDATION rather than a requirement: a credential wide enough to
  write is still held to the mechanism, so it is described, never refused.

A connector that can only learn this opportunistically (Tinybird: the Token API answers
only for a token that can administer tokens, so a properly read-scoped one cannot
enumerate its own scopes) must be able to say "not readable" rather than borrow a
stronger word.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The credential is known to be able to read data and nothing else.
TOKEN_SCOPE_READ_ONLY = "read_only"  # noqa: S105 — a classification word, not a secret
#: The credential can also append, create or drop — Alkera still only issues SELECT.
TOKEN_SCOPE_WRITE_CAPABLE = "write_capable"  # noqa: S105 — a classification word, not a secret
#: The credential administers the workspace (or can read other credentials).
TOKEN_SCOPE_ADMIN = "admin"  # noqa: S105 — a classification word, not a secret
#: The source would not say. The honest default, and the only one a static descriptor may
#: claim, because nothing about the credential in hand is known before it is used.
TOKEN_SCOPE_UNREADABLE = "unreadable"  # noqa: S105 — a classification word, not a secret

TOKEN_SCOPES = frozenset(
    {
        TOKEN_SCOPE_READ_ONLY,
        TOKEN_SCOPE_WRITE_CAPABLE,
        TOKEN_SCOPE_ADMIN,
        TOKEN_SCOPE_UNREADABLE,
    }
)


@dataclass(frozen=True)
class ReadOnlyEnforcement:
    """One connector's read-only claim, split into what is enforced and what was learned."""

    mechanism: str = ""
    """What actually keeps this connector from writing, in one sentence a UI may show
    verbatim. EMPTY when the connector has no guarantee of its own beyond the agent-side
    capability gate — an empty badge is the honest answer there."""

    token_scope: str = TOKEN_SCOPE_UNREADABLE
    """What the credential itself turned out to be able to do — one of
    :data:`TOKEN_SCOPES`. It never gates the connection (the mechanism does that); it is
    what a badge shows so an operator can see whether the credential they pasted is as
    narrow as we recommend. ``unreadable`` is the static answer, because a descriptor
    describes an engine and cannot know the credential a connection will carry."""


__all__ = [
    "TOKEN_SCOPES",
    "TOKEN_SCOPE_ADMIN",
    "TOKEN_SCOPE_READ_ONLY",
    "TOKEN_SCOPE_UNREADABLE",
    "TOKEN_SCOPE_WRITE_CAPABLE",
    "ReadOnlyEnforcement",
]

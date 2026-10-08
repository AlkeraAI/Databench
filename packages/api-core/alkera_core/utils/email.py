"""Email parsing helpers.

Inputs are expected to already be syntactically valid addresses (every API
boundary types them as ``pydantic.EmailStr``). These helpers normalize, they
do not validate.
"""

from __future__ import annotations


def email_domain(email: str) -> str:
    """Return the lowercased domain part of an email address.

    The domain is everything after the *last* ``@`` — the only place an ``@``
    may appear unescaped in an RFC 5321 address — so this is robust even for
    quoted local parts like ``"weird@local"@example.com``. Returns ``""`` for a
    string with no ``@`` (defensive; should never happen for ``EmailStr`` input).
    """
    _, sep, domain = email.rpartition("@")
    if not sep:
        return ""
    return domain.strip().lower()

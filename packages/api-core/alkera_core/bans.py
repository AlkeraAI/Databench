"""The one definition of "this account is banned".

A principal is banned when an active ban names their user id, OR an active
domain ban names the domain of their email. Every path that resolves a user —
the session dependency, a personal access token's owner, a password login, a
device-code redemption, a Slack link — asks this module the same question in
the same SQL, so the answer cannot drift between them.

``banned_predicate`` is a column expression to fold into the user fetch a path
already makes: ``select(User, banned_predicate())`` is still one statement, so
the per-request budget is unchanged. ``normalize_domain`` is the single
spelling a domain ban is stored and matched under.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import ColumnElement, SQLColumnExpression, and_, exists, or_

from alkera_core.models.ban import EmailDomainBan, UserBan
from alkera_core.models.user import User

#: A bare hostname: dot-separated labels of letters, digits and inner hyphens,
#: at least two labels (a lone TLD is never an email domain), 253 chars at most.
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+$"
)


class InvalidDomainError(ValueError):
    """The text is not a bare hostname a domain ban could match."""


def normalize_domain(raw: str) -> str:
    """The stored spelling of a domain ban: lower-case, surrounding whitespace
    and one leading ``@`` removed. Anything that is not then a bare hostname —
    a full address, a URL, a path, a lone TLD — raises
    :class:`InvalidDomainError` rather than being stored as a ban that could
    never match anyone."""
    domain = raw.strip().lower()
    if domain.startswith("@"):
        domain = domain[1:].strip()
    if not _HOSTNAME.match(domain):
        raise InvalidDomainError(f"{raw!r} is not a domain")
    return domain


def active_user_ban_exists(user_id: SQLColumnExpression[Any]) -> ColumnElement[bool]:
    """``EXISTS`` an unlifted ban naming ``user_id``."""
    return exists().where(UserBan.user_id == user_id, UserBan.lifted_at.is_(None))


def active_domain_ban_exists(domain: SQLColumnExpression[Any]) -> ColumnElement[bool]:
    """``EXISTS`` an unlifted ban naming ``domain`` (already normalized)."""
    return exists().where(EmailDomainBan.domain == domain, EmailDomainBan.lifted_at.is_(None))


def banned_predicate() -> ColumnElement[bool]:
    """Whether the ``users`` row under consideration is banned — by id, or by
    the domain of its email. Selectable beside the row in the same statement.

    A domain ban never covers a platform staff account: a ban on the
    operators' own domain must not lock every admin out with nobody left to
    lift it. A staff account cannot be banned by id either (the service
    refuses), so staff are simply never banned."""
    return or_(
        active_user_ban_exists(User.id),
        and_(User.platform_role.is_(None), active_domain_ban_exists(User.email_domain)),
    )


__all__ = [
    "InvalidDomainError",
    "active_domain_ban_exists",
    "active_user_ban_exists",
    "banned_predicate",
    "normalize_domain",
]

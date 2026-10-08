"""Which email domains an org's SSO connection speaks for: the one owner.

Platform staff assign each org its domains (``sso_domain_claims``); a domain
belongs to at most one org. Everything that acts on a domain reads
:func:`domains_of` or :func:`owner_of`: sign-in discovery, the addresses an
org's IdP may assert, SCIM provisioning and SSO enforcement. No other module
decides anything from ``sso_connections.allowed_domains``, which is only a copy
kept for the previous release (:func:`assign` writes it).

Assignment is not proof. Staff check a domain before assigning it, and a
public mailbox domain is refused by the caller, which owns that list. A proof
(a DNS record, say) can later become a state on the claim row that these
readers filter on, without changing any of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.models import SsoConnection, SsoDomainClaim
from alkera_core.models.sso_domain_claim import DOMAIN_HELD_CODE

_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


class InvalidDomainError(ValueError):
    """A value is not a domain name."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


class DomainHeldError(RuntimeError):
    """Another org already holds the domain."""

    def __init__(self, domain: str) -> None:
        super().__init__(domain)
        self.domain = domain


@dataclass(frozen=True)
class Assignment:
    """What :func:`assign` changed for the org."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    #: Whether requiring SSO was turned off because no domain is left.
    enforcement_disabled: bool


def normalize(value: str) -> str:
    """One domain, trimmed, lowercased, without a trailing dot. Raises
    :class:`InvalidDomainError` for a value that is not a domain name."""
    domain = value.strip().lower().rstrip(".")
    if _DOMAIN.fullmatch(domain) is None:
        raise InvalidDomainError(value.strip())
    return domain


def parse_domains(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """The domains in ``values``, normalized, in the order given and without
    repeats. Blank entries are skipped."""
    seen: dict[str, None] = {}
    for value in values:
        if value.strip():
            seen.setdefault(normalize(value))
    return tuple(seen)


async def domains_of(db: AsyncSession, org_team_id: UUID) -> frozenset[str]:
    """The domains assigned to the org. Empty for an org with none."""
    rows = await db.execute(
        select(SsoDomainClaim.domain).where(SsoDomainClaim.org_team_id == org_team_id)
    )
    return frozenset(rows.scalars().all())


async def listed(db: AsyncSession, org_team_id: UUID) -> tuple[str, ...]:
    """The org's domains, sorted, for display."""
    return tuple(sorted(await domains_of(db, org_team_id)))


async def owner_of(db: AsyncSession, domain: str) -> UUID | None:
    """The org the domain is assigned to, if any. At most one is."""
    row = await db.execute(
        select(SsoDomainClaim.org_team_id).where(
            SsoDomainClaim.domain == domain.strip().lower().rstrip(".")
        )
    )
    return row.scalar_one_or_none()


async def assign(
    db: AsyncSession,
    org_team_id: UUID,
    domains: tuple[str, ...],
    *,
    assigned_by_id: UUID | None,
) -> Assignment:
    """Make ``domains`` (already normalized) the org's whole set.

    Raises :class:`DomainHeldError` when another org holds one of them, and
    changes nothing then. Two assignments of one domain at once both pass that
    check; the unique constraint refuses the second, and the refusal is left
    to the database error classifier, which answers it with the same code
    (registered beside the constraint). A connection row is created when the org has none,
    so the copy the previous release reads has somewhere to live. An org left
    with no domain stops requiring SSO: its IdP can then sign nobody in, and
    the requirement would lock its members out."""
    for domain in domains:
        holder = await owner_of(db, domain)
        if holder is not None and holder != org_team_id:
            raise DomainHeldError(domain)

    current = await domains_of(db, org_team_id)
    added = tuple(d for d in domains if d not in current)
    removed = tuple(sorted(current - set(domains)))
    connection = (
        await lock_rows(
            db,
            LockRank.ORG_SETTINGS,
            select(SsoConnection).where(SsoConnection.org_team_id == org_team_id),
        )
    ).scalar_one_or_none()
    if connection is None:
        connection = SsoConnection(org_team_id=org_team_id)
        db.add(connection)
    if removed:
        await db.execute(
            delete(SsoDomainClaim).where(
                SsoDomainClaim.org_team_id == org_team_id,
                SsoDomainClaim.domain.in_(removed),
            )
        )
    for domain in added:
        db.add(
            SsoDomainClaim(org_team_id=org_team_id, domain=domain, assigned_by_id=assigned_by_id)
        )
    connection.allowed_domains = ",".join(sorted(domains))
    enforcement_disabled = not domains and connection.enforced
    if enforcement_disabled:
        connection.enforced = False
    await db.flush()
    return Assignment(added=added, removed=removed, enforcement_disabled=enforcement_disabled)


__all__ = [
    "DOMAIN_HELD_CODE",
    "Assignment",
    "DomainHeldError",
    "InvalidDomainError",
    "assign",
    "domains_of",
    "listed",
    "normalize",
    "owner_of",
    "parse_domains",
]

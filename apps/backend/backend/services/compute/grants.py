"""Compute admission: ONE resolver, ONE check, refusals that are never silent.

Credits are money; a grant is admission control. ``resolve_compute_grant`` is
the single place that walks a team's ancestor chain (leaf to org root), applies
most-specific-wins — a grant for this machine type beats a wildcard at the same
level, a nearer level beats a farther one — and returns the effective ceiling,
the billed rate and the funding account. Every start path (a session
allocation, a workspace registration) calls :func:`admit`, and nothing else
counts machines: no other concurrency check exists.

Admission refuses in exactly three ways, each a typed error the route turns
into ``402`` or ``429`` with a ``{code, message}`` body — and each is recorded
before it is raised: a ``compute_machine.changed`` frame with ``status:
refused`` and the reason (so the UI surfaces it), plus an org audit event, both
committed in a session of their own so the refused request's rollback cannot
lose them. The absence of any grant is a refusal too: nothing runs on the plane
that nobody granted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from alkera_core.authz import ActingContext
from alkera_core.compute.billing_port import compute_billing, compute_funding
from alkera_core.compute.events import announce
from alkera_core.config import settings
from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import org_root_for_team
from alkera_core.logging import get_logger
from alkera_core.models import User
from alkera_core.models.compute import (
    COMPUTE_ACTIVE_STATES,
    ComputeAllocation,
    ComputeGrant,
    ComputeMachineType,
)
from alkera_core.models.org_audit_event import OrgAuditEvent
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.audit import org_audit as org_audit_service
from backend.services.org import teams as team_service

log = get_logger(__name__)

REFUSED_AUDIT_ACTION = "compute.refused"


@dataclass(frozen=True, slots=True)
class Grant:
    """The effective admission for one caller and machine type."""

    ceiling: int
    rate_per_minute_nanos: int
    funding_account_id: UUID | None
    source_grant_id: UUID
    per_user_max: int | None
    expires_at: datetime
    machine_type_id: UUID | None
    """The grant's own scope: ``None`` when a wildcard admitted this type."""


class ComputeRefusedError(Exception):
    """A start the plane refused. ``code`` is the wire code; the route maps the
    class to its status."""

    http_status: int = 429

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def detail_extra(self) -> dict[str, object]:
        """Figures the refusal names beside its code and message."""
        return {}


class NoComputeGrantError(ComputeRefusedError):
    """No live grant admits this machine type for the caller's team chain."""

    def __init__(self) -> None:
        super().__init__(
            "no_compute_grant", "No compute grant admits this machine type for your team."
        )


class ComputeLimitReachedError(ComputeRefusedError):
    """The grant's ceiling (or the caller's per-user share of it) is in use."""

    def __init__(self, *, used: int, ceiling: int, per_user: bool = False) -> None:
        scope = "your" if per_user else "your team's"
        super().__init__(
            "compute_user_limit_reached" if per_user else "compute_limit_reached",
            f"{scope} compute limit is in use: {used}/{ceiling} machines running.",
        )


class InsufficientComputeCreditError(ComputeRefusedError):
    """The funding account cannot cover the first minute at the billed rate."""

    http_status = 402

    def __init__(self) -> None:
        super().__init__(
            "insufficient_credit",
            "Not enough credit to run this machine for a minute; add credit and try again.",
        )


async def resolve_compute_grant(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    org_team_id: UUID,
    machine_type: ComputeMachineType,
    at: datetime | None = None,
) -> Grant | None:
    """The most specific live grant on ``org_team_id``'s ancestor chain for
    ``machine_type``, with the funding account it bills, or ``None`` when no
    grant admits the type. A chain outside the caller's org resolves to nothing:
    another tenant's grants are never visible, let alone usable."""
    moment = at or datetime.now(UTC)
    chain = [team.id for team in await team_service.ancestor_chain(db, org_team_id)]
    if not chain:
        return None
    if await org_root_for_team(db, org_team_id) != ctx.org_id:
        return None
    rows = (
        (
            await db.execute(
                select(ComputeGrant).where(
                    ComputeGrant.org_team_id.in_(chain),
                    ComputeGrant.expires_at > moment,
                    (ComputeGrant.machine_type_id == machine_type.id)
                    | (ComputeGrant.machine_type_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
    )
    by_team: dict[UUID, list[ComputeGrant]] = {}
    for row in rows:
        by_team.setdefault(row.org_team_id, []).append(row)
    chosen: ComputeGrant | None = None
    for team_id in chain:  # leaf first: the nearest level wins
        level = by_team.get(team_id, [])
        typed = [g for g in level if g.machine_type_id == machine_type.id]
        wild = [g for g in level if g.machine_type_id is None]
        # Deterministic among duplicates at one level: the newest row.
        pick = max(typed or wild, key=lambda g: (g.created_at, g.id.hex), default=None)
        if pick is not None:
            chosen = pick
            break
    if chosen is None:
        return None
    funding = await compute_funding().grant_funding(db, chosen.id)
    if funding is None:
        funding = await _default_funding_account(db, ctx=ctx)
    return Grant(
        ceiling=chosen.ceiling,
        rate_per_minute_nanos=chosen.rate_per_minute_nanos,
        funding_account_id=funding,
        source_grant_id=chosen.id,
        per_user_max=chosen.per_user_max,
        expires_at=chosen.expires_at,
        machine_type_id=chosen.machine_type_id,
    )


async def _default_funding_account(db: AsyncSession, *, ctx: ActingContext) -> UUID | None:
    """The account a grant without its own funding bills — the gateway's order:
    the acting user's seat, then the org's shared pool. ``None`` only when
    neither exists yet (a priced machine is then unbillable and refused)."""
    return await compute_billing().default_funding_account(
        db, user_id=ctx.effective_user_id, org_id=ctx.org_id
    )


async def _in_use(db: AsyncSession, *, grant_id: UUID, user_id: UUID | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(ComputeAllocation)
        .where(
            ComputeAllocation.grant_id == grant_id,
            ComputeAllocation.state.in_(COMPUTE_ACTIVE_STATES),
        )
    )
    if user_id is not None:
        stmt = stmt.where(ComputeAllocation.user_id == user_id)
    return int((await db.execute(stmt)).scalar_one())


async def _can_cover_a_minute(db: AsyncSession, account_id: UUID, rate: int) -> bool:
    return await compute_billing().can_cover(db, account_id, rate)


async def admit(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    user: User,
    org_team_id: UUID,
    machine_type: ComputeMachineType,
    at: datetime | None = None,
) -> Grant:
    """The ONLY admission check on the plane. Resolves the grant, serializes on
    it, counts the machines it already admits (and the caller's share), checks
    the first minute is fundable at the billed rate, and returns the grant to
    pin prices from. Raises a :class:`ComputeRefusedError` — recorded on the
    event outbox and the org audit trail first — when the start must not happen.
    The caller must hold the row it inserts in the same transaction, so the lock
    covers count + insert."""
    grant = await resolve_compute_grant(
        db, ctx=ctx, org_team_id=org_team_id, machine_type=machine_type, at=at
    )
    if grant is None:
        raise await _refuse(NoComputeGrantError(), ctx=ctx, user=user, machine_type=machine_type)
    await advisory_xact_lock(db, advisory_key("compute-grant", grant.source_grant_id))
    used = await _in_use(db, grant_id=grant.source_grant_id)
    if used >= grant.ceiling:
        raise await _refuse(
            ComputeLimitReachedError(used=used, ceiling=grant.ceiling),
            ctx=ctx,
            user=user,
            machine_type=machine_type,
        )
    if grant.per_user_max is not None:
        mine = await _in_use(db, grant_id=grant.source_grant_id, user_id=user.id)
        if mine >= grant.per_user_max:
            raise await _refuse(
                ComputeLimitReachedError(used=mine, ceiling=grant.per_user_max, per_user=True),
                ctx=ctx,
                user=user,
                machine_type=machine_type,
            )
    if grant.rate_per_minute_nanos > 0 and (
        grant.funding_account_id is None
        or not await _can_cover_a_minute(db, grant.funding_account_id, grant.rate_per_minute_nanos)
    ):
        raise await _refuse(
            InsufficientComputeCreditError(), ctx=ctx, user=user, machine_type=machine_type
        )
    return grant


async def upsert_grant(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    machine_type_id: UUID | None,
    ceiling: int,
    rate_per_minute_nanos: int,
    expires_at: datetime,
    note: str = "",
    per_user_max: int | None = None,
    funding_account_id: UUID | None = None,
    created_by: UUID | None = None,
    now: datetime | None = None,
) -> tuple[ComputeGrant, bool]:
    """Create the grant for ``(org_team_id, machine_type_id)`` or bring the live
    one up to date — the platform-staff act of granting compute, idempotent so a
    provisioning script can run any number of times. Returns the row and
    whether it was created. A grant that has already expired is left as history
    and a fresh row is written. Flushes; the caller commits."""
    moment = now or datetime.now(UTC)
    if expires_at <= moment:
        raise ValueError("a compute grant needs an expiry in the future")
    existing = (
        await db.execute(
            select(ComputeGrant)
            .where(
                ComputeGrant.org_team_id == org_team_id,
                ComputeGrant.machine_type_id == machine_type_id
                if machine_type_id is not None
                else ComputeGrant.machine_type_id.is_(None),
                ComputeGrant.expires_at > moment,
            )
            .order_by(ComputeGrant.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.ceiling = ceiling
        existing.rate_per_minute_nanos = rate_per_minute_nanos
        existing.expires_at = expires_at
        existing.note = note
        existing.per_user_max = per_user_max
        await db.flush()
        await compute_funding().set_grant_funding(db, existing.id, funding_account_id)
        return existing, False
    grant = ComputeGrant(
        org_team_id=org_team_id,
        machine_type_id=machine_type_id,
        ceiling=ceiling,
        rate_per_minute_nanos=rate_per_minute_nanos,
        expires_at=expires_at,
        note=note,
        per_user_max=per_user_max,
        created_by=created_by,
    )
    db.add(grant)
    await db.flush()
    await compute_funding().set_grant_funding(db, grant.id, funding_account_id)
    return grant, True


async def list_grants(
    db: AsyncSession, *, org_team_id: UUID, now: datetime | None = None
) -> list[ComputeGrant]:
    """Every LIVE grant written anywhere in the org's tree, newest first.

    Expired rows are history and are left out: the operator console answers
    "what may this org run right now", and a lapsed grant admits nothing.
    """
    moment = now or datetime.now(UTC)
    team_ids = [org_team_id, *await team_service.descendant_ids(db, org_team_id)]
    rows = await db.execute(
        select(ComputeGrant)
        .where(ComputeGrant.org_team_id.in_(team_ids), ComputeGrant.expires_at > moment)
        .order_by(ComputeGrant.created_at.desc())
    )
    return list(rows.scalars().all())


async def revoke_grant(db: AsyncSession, *, grant_id: UUID, org_team_id: UUID) -> bool:
    """Delete one grant, but only if it belongs to the named org's tree.

    The org check is the tenancy guard on a cross-tenant route: a grant id from
    another org must read as "no such grant here", never as a successful
    revoke. Returns whether a row was removed; flushes, the caller commits.
    """
    grant = await db.get(ComputeGrant, grant_id)
    if grant is None:
        return False
    if grant.org_team_id != org_team_id and grant.org_team_id not in set(
        await team_service.descendant_ids(db, org_team_id)
    ):
        return False
    await db.delete(grant)
    await db.flush()
    return True


async def _recently_recorded(
    session: AsyncSession,
    *,
    org_id: UUID,
    machine_type: ComputeMachineType,
    reason: str,
    now: datetime,
) -> bool:
    """Whether this exact refusal (org x machine type x reason) is already on
    record inside the window.

    A refusal is an ANSWER, not a transient failure: no grant, no credit, the
    ceiling in use. A box that keeps asking — a mirror re-registering, a browser
    retrying — would write one frame and one audit row per attempt, which buries
    the trail instead of making it legible and re-invalidates every open browser
    each time. The FIRST refusal in each window is recorded in full; the repeats
    are answered exactly the same way to the caller and simply not re-filed."""
    window = settings.compute_refusal_record_window_seconds
    if window <= 0:
        return False
    since = now - timedelta(seconds=window)
    hit = (
        await session.execute(
            select(OrgAuditEvent.id)
            .where(
                OrgAuditEvent.org_team_id == org_id,
                OrgAuditEvent.action == REFUSED_AUDIT_ACTION,
                OrgAuditEvent.target == machine_type.provider_type_id,
                OrgAuditEvent.detail["reason"].astext == reason,
                OrgAuditEvent.created_at >= since,
            )
            .limit(1)
        )
    ).first()
    return hit is not None


async def _refuse(
    error: ComputeRefusedError,
    *,
    ctx: ActingContext,
    user: User,
    machine_type: ComputeMachineType,
) -> ComputeRefusedError:
    """Put the refusal on record — the ``refused`` frame the UI surfaces and the
    org audit event — in a session of its own, so the refused request's rollback
    cannot lose either. Returns ``error`` for the caller to raise. A failure to
    record is logged and never changes what the refusal looks like. An identical
    refusal already on record inside the window is not filed again (see
    :func:`_recently_recorded`); the caller still gets the same error."""
    try:
        async with AsyncSessionLocal() as session:
            if await _recently_recorded(
                session,
                org_id=ctx.org_id,
                machine_type=machine_type,
                reason=error.code,
                now=datetime.now(UTC),
            ):
                log.info(
                    "compute.refusal.already_on_record",
                    reason=error.code,
                    org_id=str(ctx.org_id),
                    machine_type=machine_type.provider_type_id,
                )
                return error
            await announce(
                session,
                org_id=ctx.org_id,
                entity_id=str(ctx.org_id),
                status="refused",
                reason=error.code,
                actor=ctx.audit_dict(),
            )
            await org_audit_service.record(
                session,
                org_id=ctx.org_id,
                actor=user,
                action=REFUSED_AUDIT_ACTION,
                target=machine_type.provider_type_id,
                detail={
                    "reason": error.code,
                    "machine_type_id": str(machine_type.id),
                    "machine_type": machine_type.display_name,
                },
                acting=ctx,
            )
            await session.commit()
    except Exception:  # the refusal reaches the caller whatever the record does
        log.error("compute.refusal.record_failed", reason=error.code, exc_info=True)
    return error


# -- machines an org holds: grant sources ------------------------------------


@dataclass(frozen=True, slots=True)
class MachineGrantQuery:
    """What a machine admission asks the grant sources about."""

    ctx: ActingContext
    org_id: UUID
    machine_type: ComputeMachineType
    at: datetime


@dataclass(frozen=True, slots=True)
class MachineGrant:
    """What admits an org machine: how many machines the org may hold, and the
    compute rate to pin (``None`` takes the offering's). Admissions serialize
    on the org's machine count, whatever grant admits them. Who pays is never the grant's to say: an
    org machine is funded from its owner team's pool, else the org's, never a
    member's seat."""

    source: str
    ceiling: int
    rate_per_minute_nanos: int | None
    source_grant_id: UUID | None = None


MachineGrantSource = Callable[[AsyncSession, MachineGrantQuery], Awaitable[MachineGrant | None]]

_MACHINE_GRANT_SOURCES: dict[str, MachineGrantSource] = {}


def register_machine_grant_source(name: str, source: MachineGrantSource) -> None:
    """Register a source of machine grants. Sources are asked in registration
    order and the first that answers admits; re-registering a name replaces
    it in place."""
    _MACHINE_GRANT_SOURCES[name] = source


def machine_grant_sources() -> tuple[str, ...]:
    return tuple(_MACHINE_GRANT_SOURCES)


async def resolve_machine_grant(db: AsyncSession, query: MachineGrantQuery) -> MachineGrant | None:
    """The first registered source's answer for ``query``, or ``None``."""
    for source in _MACHINE_GRANT_SOURCES.values():
        grant = await source(db, query)
        if grant is not None:
            return grant
    return None


async def _explicit_grant_source(db: AsyncSession, query: MachineGrantQuery) -> MachineGrant | None:
    """A ``compute_grants`` row on the org: its ceiling, and its rate, which
    overrides the offering's (a negotiated price)."""
    grant = await resolve_compute_grant(
        db,
        ctx=query.ctx,
        org_team_id=query.org_id,
        machine_type=query.machine_type,
        at=query.at,
    )
    if grant is None:
        return None
    return MachineGrant(
        source="compute_grant",
        ceiling=grant.ceiling,
        rate_per_minute_nanos=grant.rate_per_minute_nanos,
        source_grant_id=grant.source_grant_id,
    )


async def _plan_default_source(db: AsyncSession, query: MachineGrantQuery) -> MachineGrant | None:
    """The org's plan: its machine quota, at the offering's rate."""
    return MachineGrant(
        source="plan",
        ceiling=await compute_billing().machine_quota(db, query.org_id),
        rate_per_minute_nanos=None,
    )


register_machine_grant_source("compute_grant", _explicit_grant_source)
register_machine_grant_source("plan", _plan_default_source)


async def record_refusal(
    error: ComputeRefusedError,
    *,
    ctx: ActingContext,
    user: User,
    machine_type: ComputeMachineType,
) -> ComputeRefusedError:
    """Put a refusal on record the way every admission does (see
    :func:`_refuse`) and return it for the caller to raise."""
    return await _refuse(error, ctx=ctx, user=user, machine_type=machine_type)


__all__ = [
    "REFUSED_AUDIT_ACTION",
    "ComputeLimitReachedError",
    "ComputeRefusedError",
    "Grant",
    "InsufficientComputeCreditError",
    "MachineGrant",
    "MachineGrantQuery",
    "MachineGrantSource",
    "NoComputeGrantError",
    "admit",
    "list_grants",
    "machine_grant_sources",
    "record_refusal",
    "register_machine_grant_source",
    "resolve_compute_grant",
    "resolve_machine_grant",
    "revoke_grant",
    "upsert_grant",
]

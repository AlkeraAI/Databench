"""Platform machine credentials: mint, resolve, claim, revoke, list.

The box-bound counterpart of the CI, proxy and personal access token services.
A credential is minted by a platform admin for ONE named box, with the catalog
row (kind + size) and the tenancy (``pool`` or ``dedicated``) fixed at mint,
so a box can never say what it is: it presents the credential and the plane
already knows. The raw secret exists only in the mint return value; the row
stores its HMAC-SHA256 digest under the shared lookup-token pepper, so a
presented credential is matched with ``token_hash IN (active digest,
*previous-pepper digests)`` and a pepper rotation never strands a box.

Resolution admits a credential only while it is unrevoked; a claim binds it to
the machine row the box registered, and from then on a request on it for any
other machine is refused. A machine is held by one live credential at a time:
a claim under a new one retires the old.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from alkera_core.auth.machine_token import mint_machine_token
from alkera_core.auth.token_hash import lookup_token_digests
from alkera_core.compute.personal import PERSONAL_MACHINE_TYPE_ID
from alkera_core.compute.provider import PERSONAL
from alkera_core.models import MachineCredential, User
from alkera_core.models.compute import PERSONAL_TENANCY, ComputeAllocation, ComputeMachineType
from alkera_core.models.machine_credential import MACHINE_LABEL_MAX, PLATFORM_TENANCIES
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def mint(
    db: AsyncSession,
    *,
    org_id: UUID,
    created_by: UUID,
    machine_type: ComputeMachineType,
    tenancy: str,
    label: str,
    region: str = "",
) -> tuple[MachineCredential, str]:
    """Create a credential for a box of ``machine_type``. Returns
    ``(row, raw_secret)``; the raw secret is never persisted. Flushes; the
    caller commits."""
    if tenancy not in PLATFORM_TENANCIES:
        raise ValueError(f"a platform box is pool or dedicated, not {tenancy!r}")
    if not machine_type.active:
        raise ValueError("the machine type is not active")
    raw, token_hash = mint_machine_token()
    row = MachineCredential(
        token_hash=token_hash,
        label=label,
        org_team_id=org_id,
        machine_type_id=machine_type.id,
        region=region,
        tenancy=tenancy,
        created_by=created_by,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row, raw


async def personal_machine_type(db: AsyncSession) -> ComputeMachineType:
    """The one catalog row every personal box registers as: provider
    ``personal``, never active in the catalog, never offered for a new
    allocation, priced at nothing (a person's own hardware is not billed).
    Made on first use; a concurrent first use lands on the same row."""
    wanted = (ComputeMachineType.provider == PERSONAL) & (
        ComputeMachineType.provider_type_id == PERSONAL_MACHINE_TYPE_ID
    )
    row = (await db.execute(select(ComputeMachineType).where(wanted))).scalar_one_or_none()
    if row is not None:
        return row
    await db.execute(
        pg_insert(ComputeMachineType)
        .values(
            id=uuid4(),
            provider=PERSONAL,
            provider_type_id=PERSONAL_MACHINE_TYPE_ID,
            display_name="Personal box",
            provider_price_per_minute_nanos=0,
            active=False,
            available_for_new=False,
        )
        .on_conflict_do_nothing(constraint="uq_compute_machine_types_provider_type")
    )
    return (await db.execute(select(ComputeMachineType).where(wanted))).scalar_one()


async def mint_personal(
    db: AsyncSession, *, owner: User, org_id: UUID, label: str
) -> tuple[MachineCredential, str]:
    """A machine credential for ``owner``'s own box, bound to ``org_id`` (the
    org the approving session was in) and to them (``created_by``): the box serves
    their own private chats in that org and nothing else, and stands only while
    they remain an active member of it. Returns ``(row, raw_secret)``; flushes,
    the caller commits."""
    machine_type = await personal_machine_type(db)
    raw, token_hash = mint_machine_token()
    row = MachineCredential(
        token_hash=token_hash,
        label=label[:MACHINE_LABEL_MAX],
        org_team_id=org_id,
        machine_type_id=machine_type.id,
        tenancy=PERSONAL_TENANCY,
        created_by=owner.id,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row, raw


async def resolve_active(
    db: AsyncSession, raw: str, *, now: datetime | None = None
) -> MachineCredential | None:
    """The live (unrevoked) credential matching ``raw``, or ``None``."""
    del now  # a machine credential does not expire; it is revoked
    row = (
        await db.execute(
            select(MachineCredential).where(
                MachineCredential.token_hash.in_(lookup_token_digests(raw))
            )
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    return row


async def get(db: AsyncSession, credential_id: UUID) -> MachineCredential | None:
    return await db.get(MachineCredential, credential_id)


async def claim(
    db: AsyncSession,
    credential: MachineCredential,
    alloc: ComputeAllocation,
    *,
    now: datetime | None = None,
) -> None:
    """The box that registered ``alloc`` holds this credential from now on —
    and ONLY this one: any other live credential that held the same machine
    is revoked here. A claim under a freshly minted credential is how a box's
    secret is rotated, and a rotation that left the old secret working would
    rotate nothing. The retired row keeps its ``machine_id`` so the machines
    page still shows which box it belonged to."""
    credential.machine_id = alloc.id
    await db.execute(
        update(MachineCredential)
        .where(
            MachineCredential.machine_id == alloc.id,
            MachineCredential.id != credential.id,
            MachineCredential.revoked_at.is_(None),
        )
        .values(revoked_at=now or datetime.now(UTC))
    )
    await db.flush()


async def revoke(
    db: AsyncSession, credential_id: UUID, *, now: datetime | None = None
) -> MachineCredential | None:
    """Revoke a credential; idempotent. Returns the row, ``None`` when there is
    no such credential. The caller commits."""
    row = await db.get(MachineCredential, credential_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = now or datetime.now(UTC)
        await db.flush()
    return row


async def list_personal(
    db: AsyncSession, owner_id: UUID, *, org_id: UUID
) -> list[MachineCredential]:
    """Every box ever set up on ``owner_id``'s own hardware in ``org_id``,
    newest first, revoked ones included (a box taken away reads as taken
    away). A box is bound to the org it was approved in, and a person's
    credential for one org reaches nothing of another's."""
    rows = await db.execute(
        select(MachineCredential)
        .where(
            MachineCredential.tenancy == PERSONAL_TENANCY,
            MachineCredential.created_by == owner_id,
            MachineCredential.org_team_id == org_id,
        )
        .order_by(MachineCredential.created_at.desc())
    )
    return list(rows.scalars().all())


async def list_all(db: AsyncSession) -> list[MachineCredential]:
    """Every credential ever minted, newest first — revoked ones included, so
    the machines page shows a box that was taken away as taken away rather
    than as never having existed."""
    rows = await db.execute(select(MachineCredential).order_by(MachineCredential.created_at.desc()))
    return list(rows.scalars().all())


__all__ = [
    "claim",
    "get",
    "list_all",
    "list_personal",
    "mint",
    "mint_personal",
    "personal_machine_type",
    "resolve_active",
    "revoke",
]

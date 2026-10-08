"""The credential a provisioned box boots with: its own machine credential,
and nothing else.

A node's secret used to carry the provisioning admin's 90-day CLI token beside
the machine credential, so a box that reached the API as "the admin" could do
everything the admin could, on every route, in every org. The shape here
carries the one credential the plane minted for that box: ``alk_machine_``
resolves to the machine principal, which a policy admits for exactly the
machine and the chats the credential is bound to. A node secret holds no user
session; a route that still needs one is that route's gap, never the node's.

:func:`node_credential_for` is the seam provisioning calls. It mints the
credential bound to the allocation, retires any live credential the same
machine held before (a re-issue rotates — it never leaves two secrets
working), and returns the raw secret in the exact environment shape the
bootstrap script reads into the box.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from alkera_core.auth.machine_token import looks_like_machine_token
from alkera_core.models import MachineCredential
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.credentials import machine_credentials as machine_credential_service

#: The one key a node's secret carries. The bootstrap reads it into the box's
#: environment, and the daemon presents it as the bearer on every call.
NODE_CREDENTIAL_KEY = "ALKERA_MACHINE_CREDENTIAL"


@dataclass(frozen=True, slots=True)
class NodeCredential:
    """What provisioning ships to a node, and the row that backs it."""

    credential: MachineCredential
    #: The environment the node boots with: exactly one key, the raw secret.
    secrets: dict[str, str]

    @property
    def raw(self) -> str:
        return self.secrets[NODE_CREDENTIAL_KEY]


async def node_credential_for(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    created_by: UUID | None = None,
    region: str = "",
    now: datetime | None = None,
) -> NodeCredential:
    """Mint the credential ``alloc`` boots with, bound to it.

    The credential is the allocation's in every respect a box could otherwise
    misstate: its org is the operator org that provisioned the node, its
    tenancy, label and machine type are the allocation's. ``created_by`` is
    the admin who asked, defaulting to the allocation's own user. Any live
    credential already bound to this machine is revoked first, so a node whose
    secret is re-issued holds one credential, never two.

    Flushes and does not commit: the caller's transaction owns the row, so a
    provision that fails after this rolls the credential back with everything
    else instead of leaving a live secret for a node that never ran.
    """
    machine_type = await db.get(ComputeMachineType, alloc.machine_type_id)
    if machine_type is None:
        raise ValueError("the allocation names no machine type")
    author = created_by if created_by is not None else alloc.user_id
    moment = now or datetime.now(UTC)
    await db.execute(
        update(MachineCredential)
        .where(MachineCredential.machine_id == alloc.id, MachineCredential.revoked_at.is_(None))
        .values(revoked_at=moment)
    )
    credential, raw = await machine_credential_service.mint(
        db,
        org_id=alloc.org_team_id,
        created_by=author,
        machine_type=machine_type,
        tenancy=alloc.tenancy,
        label=alloc.name or f"node-{alloc.id.hex[:8]}",
        region=region,
    )
    credential.machine_id = alloc.id
    await db.flush()
    if not looks_like_machine_token(raw):
        # The minter's contract, re-checked at the one place the secret leaves
        # the plane: a node must never boot with something the resolver would
        # route to a person.
        raise ValueError("the minted secret is not a machine credential")
    return NodeCredential(credential=credential, secrets={NODE_CREDENTIAL_KEY: raw})


__all__ = ["NODE_CREDENTIAL_KEY", "NodeCredential", "node_credential_for"]

"""Where the SSH provider reads a host's details, and the one way a credential
is sealed into a row and opened out of it.

The credential is sealed with :mod:`alkera_core.auth.secret_box` (the store
the per-allocation SSH keys and SSO client secrets use) as one JSON document,
so a password and a passphrase can never be read apart from the kind they
belong to. :func:`seal_credential` is the only way in and
:func:`open_credential` the only way out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, update

from alkera_core.auth.secret_box import InvalidToken, decrypt_secret, encrypt_secret
from alkera_core.compute.ssh.transport import SSH_AUTH_KINDS, SshAuth
from alkera_core.models.compute import ComputeAllocation
from alkera_core.models.org_machines import OrgMachine
from alkera_core.models.ssh_machines import SshMachineEndpoint


def seal_credential(auth: SshAuth) -> str:
    """The ciphertext a credential is stored as."""
    if auth.kind not in SSH_AUTH_KINDS or not auth.secret:
        raise ValueError("an SSH credential needs a kind and a secret")
    document = {"kind": auth.kind, "secret": auth.secret, "passphrase": auth.passphrase}
    return encrypt_secret(json.dumps(document))


def open_credential(sealed: str) -> SshAuth | None:
    """The credential a row holds; ``None`` once it was forgotten or when no
    configured key opens it."""
    if not sealed:
        return None
    try:
        document = json.loads(decrypt_secret(sealed))
    except (InvalidToken, ValueError):
        return None
    kind = document.get("kind")
    if kind not in SSH_AUTH_KINDS:
        return None
    return SshAuth(
        kind=kind,
        secret=str(document.get("secret") or ""),
        passphrase=str(document.get("passphrase") or ""),
    )


@dataclass(frozen=True)
class Endpoint:
    """One attached host as the provider needs it."""

    id: UUID
    org_id: UUID
    org_machine_id: UUID
    host: str
    port: int
    username: str
    host_key: str
    #: ``None`` once the credential was forgotten.
    auth: SshAuth | None
    #: Whether the org machine it backs was removed.
    machine_deleted: bool
    #: When it was removed; ``None`` while it is not.
    machine_deleted_at: datetime | None = None
    #: The host's ``uname -m`` as read when it was added.
    arch: str = ""


class EndpointStore(Protocol):
    async def get(self, endpoint_id: UUID) -> Endpoint | None: ...

    async def for_allocation(self, allocation_id: UUID) -> Endpoint | None: ...

    async def forget(self, endpoint_id: UUID) -> None:
        """Clear the stored credential."""
        ...

    async def abandon(self, endpoint: Endpoint, note: str) -> None:
        """Give up on uninstalling from a removed machine's host: clear the
        credential and put ``note`` on the org's audit record."""
        ...


def _endpoint(row: SshMachineEndpoint, machine: OrgMachine | None) -> Endpoint:
    return Endpoint(
        id=row.id,
        org_id=row.org_team_id,
        org_machine_id=row.org_machine_id,
        host=row.host,
        port=row.port,
        username=row.username,
        host_key=row.host_key,
        auth=open_credential(row.secret_sealed),
        machine_deleted=machine is None or machine.deleted_at is not None,
        machine_deleted_at=machine.deleted_at if machine is not None else None,
        arch=row.arch,
    )


class DbEndpointStore:
    """Reads ``ssh_machine_endpoints`` in a session of its own: the provider is
    called from a reconcile whose session holds row locks it must not commit.

    The session module is imported when a call is made, not at load: the
    provider registry loads this module in every process that names a
    provider (the CLI included), and importing the session builds the engine."""

    async def get(self, endpoint_id: UUID) -> Endpoint | None:
        from alkera_core.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            found = (
                await db.execute(
                    select(SshMachineEndpoint, OrgMachine)
                    .join(
                        OrgMachine,
                        (OrgMachine.id == SshMachineEndpoint.org_machine_id)
                        & (OrgMachine.org_team_id == SshMachineEndpoint.org_team_id),
                        isouter=True,
                    )
                    .where(SshMachineEndpoint.id == endpoint_id)
                )
            ).first()
        return None if found is None else _endpoint(found[0], found[1])

    async def for_allocation(self, allocation_id: UUID) -> Endpoint | None:
        from alkera_core.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            found = (
                await db.execute(
                    select(SshMachineEndpoint, OrgMachine)
                    .join(
                        ComputeAllocation,
                        (ComputeAllocation.org_machine_id == SshMachineEndpoint.org_machine_id)
                        & (ComputeAllocation.tenant_org_id == SshMachineEndpoint.org_team_id),
                    )
                    .join(
                        OrgMachine,
                        (OrgMachine.id == SshMachineEndpoint.org_machine_id)
                        & (OrgMachine.org_team_id == SshMachineEndpoint.org_team_id),
                    )
                    .where(ComputeAllocation.id == allocation_id)
                )
            ).first()
        return None if found is None else _endpoint(found[0], found[1])

    async def forget(self, endpoint_id: UUID) -> None:
        from alkera_core.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(SshMachineEndpoint)
                .where(SshMachineEndpoint.id == endpoint_id)
                .values(secret_sealed="")
            )
            await db.commit()

    async def abandon(self, endpoint: Endpoint, note: str) -> None:
        from alkera_core.audit_chain import append_row
        from alkera_core.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(SshMachineEndpoint)
                .where(SshMachineEndpoint.id == endpoint.id)
                .values(secret_sealed="")
            )
            await append_row(
                db,
                org_id=endpoint.org_id,
                actor_id=None,
                actor_email="",
                action=REMOVAL_ABANDONED,
                target=str(endpoint.org_machine_id),
                detail={"host": endpoint.host, "port": endpoint.port, "message": note},
            )
            await db.commit()


#: The audit action written when a removed machine's host never answered.
REMOVAL_ABANDONED = "machine.removal_abandoned"


__all__ = [
    "REMOVAL_ABANDONED",
    "DbEndpointStore",
    "Endpoint",
    "EndpointStore",
    "open_credential",
    "seal_credential",
]

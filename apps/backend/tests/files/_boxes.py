"""A provisioned box, as the drive will admit one.

The Files routes take an agent assertion as a claim and nothing more: the two
headers ride on the caller's own JWT and a box's machine id is published on
every chat it serves, so a test that merely sends them is a member spelling a
public string, not a machine. What makes a request a machine is the
registration — a live workspace machine of this org, registered by the user
behind the request, on the very credential the request carries — and that is
what :func:`registered_box` builds.

Spelled here rather than in one suite because several suites need both sides of
the distinction in the same file: the proven box, and the member who knows
everything about it that the product serves. Imported as ``tests.files._boxes``
so the CLI suites that drive a real box over the wire reach the same seam the
backend route tests do — a second copy of the registration is a second thing to
get wrong, and what it builds is the one shape the drive admits.
"""

from __future__ import annotations

import uuid
from typing import Any

from alkera_core.models.workspace_object import WorkspaceObject
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def registered_box(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    email: str,
    org_id: uuid.UUID,
    machine_id: uuid.UUID | None = None,
    platform: bool = False,
) -> tuple[str, str]:
    """A box credential and the live workspace machine registered on it.

    Returns ``(token, machine_id)``: the bearer token the box speaks with, and
    the id it asserts. Only this pair passes ``verify_machine_assertion`` —
    another org's machine, a colleague's session, the operator's other sessions
    and a machine id that names no row all read as unverified.

    ``machine_id`` names the allocation's id rather than letting it default,
    which is how a test builds the collision the holder fence's kind half exists
    for: a machine and a user are UUIDs from two different tables, and nothing
    stops one value appearing in both.

    ``platform`` makes it a platform box dedicated to ``org_id`` and held by a
    live machine credential (as ``/machines/claim`` and the console leave
    one), so a test can take the box away by revoking that credential.
    """
    from alkera_core.auth import encode_cli_token, register_token
    from alkera_core.compute.machines import WORKSPACE
    from alkera_core.models import ComputeAllocation, TokenType
    from alkera_core.models.compute import COMPUTE_ACTIVE_STATES
    from tests._compute_helpers import make_machine_type

    token, claims = encode_cli_token(
        user_id=user_id, email=email, org_team_id=org_id, platform_role=None
    )
    await register_token(session, claims=claims, token_type=TokenType.CLI)
    machine_type = await make_machine_type(session)
    machine = ComputeAllocation(
        **({} if machine_id is None else {"id": machine_id}),
        user_id=user_id,
        org_team_id=org_id,
        machine_type_id=machine_type.id,
        lifecycle=WORKSPACE,
        state=COMPUTE_ACTIVE_STATES[-1],
        registered_jti=str(claims.jti),
        **({"tenancy": "dedicated"} if platform else {}),
    )
    session.add(machine)
    await session.flush()
    if platform:
        from alkera_core.auth.machine_token import mint_machine_token
        from alkera_core.models import MachineCredential, OrgComputeAssignment

        _raw, digest = mint_machine_token()
        session.add(
            MachineCredential(
                token_hash=digest,
                label="box",
                org_team_id=org_id,
                machine_type_id=machine_type.id,
                tenancy="dedicated",
                machine_id=machine.id,
            )
        )
        session.add(OrgComputeAssignment(org_team_id=org_id, machine_id=machine.id))
    await session.commit()
    return token, str(machine.id)


async def credential_box(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[str, str]:
    """A box on its own machine credential, dedicated to ``org_id``.

    Returns ``(credential, machine_id)``: the ``alk_machine_…`` bearer the box
    speaks with — there is no user session behind it — and the live workspace
    machine the credential holds, assigned to the org. The shape the platform
    leaves after ``/machines/claim``: a member of no org, admitted to the
    drive only through the chats bound to its machine.
    """
    from alkera_core.compute.machines import WORKSPACE
    from alkera_core.models import ComputeAllocation, OrgComputeAssignment
    from alkera_core.models.compute import READY
    from backend.services.credentials import machine_credentials as machine_credential_service
    from tests._compute_helpers import make_machine_type

    machine_type = await make_machine_type(session)
    credential, raw = await machine_credential_service.mint(
        session,
        org_id=org_id,
        created_by=user_id,
        machine_type=machine_type,
        tenancy="dedicated",
        label="box",
    )
    machine = ComputeAllocation(
        user_id=user_id,
        org_team_id=org_id,
        machine_type_id=machine_type.id,
        lifecycle=WORKSPACE,
        state=READY,
        tenancy="dedicated",
        provider_machine_id="",
    )
    session.add(machine)
    await session.flush()
    credential.machine_id = machine.id
    session.add(OrgComputeAssignment(org_team_id=org_id, machine_id=machine.id))
    await session.commit()
    return raw, str(machine.id)


async def bind_chat(
    fx: Any, session: AsyncSession, chat: Any, *, machine: str | None, title: str = "Kickoff"
) -> None:
    """Give a chat folder the chat row behind it, running on ``machine``.

    A chat's folder is a pointer: what the folder IS lives in the object it
    names, and the machine the conversation runs on is a field of that object.
    ``machine=None`` is the state a chat sits in before any box has taken it —
    placed, named, bound to nothing.
    """
    chat_object = WorkspaceObject(
        org_team_id=fx.org_team_id,
        logical_id=uuid.uuid4().hex,
        type="chat",
        title=title,
        owner_user_id=fx.actor_id,
        visibility_scope="private",
        spec={"machine_id": machine} if machine is not None else {},
    )
    session.add(chat_object)
    await session.flush()
    await session.execute(
        text("UPDATE file_nodes SET target_object_id = :object WHERE id = :node"),
        {"object": chat_object.id, "node": chat.id},
    )
    await session.commit()


async def chat_on_box(
    session: AsyncSession, *, org_id: uuid.UUID, owner_id: uuid.UUID, machine: str
) -> uuid.UUID:
    """A chat of ``org_id`` bound to box ``machine``, with no folder: the
    work an org-bound worker credential needs its box to hold in the org,
    for a test about something else the worker does."""
    chat_object = WorkspaceObject(
        org_team_id=org_id,
        logical_id=uuid.uuid4().hex,
        type="chat",
        title="On the box",
        owner_user_id=owner_id,
        visibility_scope="private",
        spec={"machine_id": machine},
    )
    session.add(chat_object)
    await session.commit()
    return chat_object.id


__all__ = ["bind_chat", "chat_on_box", "credential_box", "registered_box"]

"""A box on its machine credential reaches a live document in the org it serves.

A pool box's credential is minted in its operator org and serves customers'
orgs. The live-document helpers once scoped every read to the caller's own
org, so for a box serving a customer the notebook "was not there": its ops
answered 404 although the file was on the drive. They now read in the org of
the node's drive, admitted only when the credential serves that org and, for
a pool box (which serves every org), only while it holds work there.

Real Postgres and real Files; the box is an acting context the way the
machine-credential door builds one.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.authz import ActingContext
from alkera_core.files.leases import LeaseConflict, LeaseContext, holder_identity
from backend.services.chats import chat_service
from backend.services.files import (
    NotEditableError,
    document_access,
    head_etag,
    holder_peer,
)
from backend.services.notebooks import box_follows
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.crdt.file_world import FileWorld, acting
from tests.crdt.test_nbdoc_session import notebook_world

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

#: The operator org a pool box's credential is minted in: never the customer's.
OPERATOR_ORG = uuid.UUID("00000000-0000-4000-8000-0000000000a1")


def _box(*, serves: frozenset[uuid.UUID], every: bool = False) -> ActingContext:
    machine = uuid.uuid4()
    return ActingContext.for_machine(
        machine_id=machine,
        credential_id=machine,
        org_id=OPERATOR_ORG,
        label="box",
        served_org_ids=serves,
        serves_every_org=every,
    )


async def _holding_work(db: AsyncSession, box: ActingContext, fw: FileWorld) -> None:
    """Bind a chat of the drive's org to the box, as placement does: a pool
    box stands in an org only while it holds work there."""
    await chat_service.create_chat(
        db,
        owner=fw.world.owner.user,
        org_id=fw.world.org_id,
        title="On the box",
        client_id=None,
        machine_id=box.acting_principal.id,
        machine_status="ready",
    )
    await db.commit()


@pytest.fixture
async def fw(real_session: AsyncSession, org_admin: OrgWithAdmin) -> FileWorld:
    return await notebook_world(real_session, org_admin)


@pytest.mark.parametrize(
    "every", [pytest.param(False, id="assigned-box"), pytest.param(True, id="pool-box")]
)
async def test_a_box_serving_the_drives_org_reads_the_file_in_that_org(
    real_session: AsyncSession, fw: FileWorld, every: bool
) -> None:
    box = _box(serves=frozenset() if every else frozenset({fw.world.org_id}), every=every)
    if every:
        await _holding_work(real_session, box, fw)
    assert await head_etag(real_session, box, fw.node_id) is not None


@pytest.mark.parametrize(
    "every", [pytest.param(False, id="assigned-box"), pytest.param(True, id="pool-box")]
)
async def test_a_box_serving_the_drives_org_is_asked_for_its_fence_not_told_the_file_is_gone(
    real_session: AsyncSession, fw: FileWorld, every: bool
) -> None:
    """The notebook ops route maps ``gone`` to its 404. A box that serves the
    org but names no lease is refused as a writer without a fence, which is
    the route's 409, never as though the notebook were missing."""
    box = _box(serves=frozenset() if every else frozenset({fw.world.org_id}), every=every)
    if every:
        await _holding_work(real_session, box, fw)
    with pytest.raises(LeaseConflict):
        await holder_peer(real_session, box, fw.node_id, lease=None)


async def test_a_box_not_serving_the_drives_org_reaches_nothing(
    real_session: AsyncSession, fw: FileWorld
) -> None:
    stranger = _box(serves=frozenset({uuid.uuid4()}))
    assert await head_etag(real_session, stranger, fw.node_id) is None
    with pytest.raises(NotEditableError) as refused:
        await holder_peer(real_session, stranger, fw.node_id, lease=None)
    assert refused.value.reason == "gone"


async def test_a_box_naming_a_node_that_is_not_there_reaches_nothing(
    real_session: AsyncSession, fw: FileWorld
) -> None:
    box = _box(serves=frozenset({fw.world.org_id}))
    assert await head_etag(real_session, box, uuid.uuid4()) is None


async def test_a_person_still_reads_in_their_own_org(
    real_session: AsyncSession, fw: FileWorld
) -> None:
    owner = acting(fw.world.owner)
    assert await head_etag(real_session, owner, fw.node_id) is not None
    with pytest.raises(LeaseConflict):
        await holder_peer(real_session, owner, fw.node_id, lease=None)


async def test_a_pool_box_with_no_binding_or_lease_in_the_org_is_refused(
    real_session: AsyncSession, fw: FileWorld
) -> None:
    """Serving every org is no standing in any of them: a pool box that runs
    none of the org's chats or workspaces and holds no lease there may not
    read the document, is not the folder's holder, and is told the file is
    gone even under a fence it claims, as a box that serves no such org is."""
    pool = _box(serves=frozenset(), every=True)
    access = await document_access(real_session, pool, fw.node_id)
    assert (access.can_read, access.can_write) == (False, False)
    assert await box_follows(real_session, pool, fw.node_id) is None
    machine = pool.acting_principal.id
    claimed = LeaseContext(
        epoch=1,
        instance_id="not-a-held-instance",
        base_known=True,
        holder=holder_identity(pool, verified_machine_id=machine),
    )
    with pytest.raises(NotEditableError) as refused:
        await holder_peer(real_session, pool, fw.node_id, lease=claimed)
    assert refused.value.reason == "gone"

"""Purging a file deletes its live document under the role the request was in.

The live-document tables are not the Files role's, so the purge steps out of
that role for its two deletes. On a session bound to its orgs the role it
steps out to must be the tenant role the request runs under, never ``NONE``:
``NONE`` is the login, which row security does not bind, and the deletes would
run with the tenant policy lifted. A session nobody bound (a background sweep)
has no tenant role to return to and steps out to the login, as before.

What the deletes ran as is read off the database itself: a row trigger on
``crdt_docs`` records ``current_user`` for every row it deletes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from alkera_core.db.tenant_session import INFO_KEY, TENANT_ROLE, bind_tenant
from alkera_core.files import NodeId
from alkera_core.files.trash import Trash
from alkera_core.models import CrdtDoc
from backend.services.files.context import build_files_context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401 -- a fixture this module uses
from tests.conftest import OrgWithAdmin
from tests.crdt.file_world import FileWorld, acting, file_world, node_of

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

_LOG = "trash_role_window_log"


@pytest.fixture
async def deletes_recorded(real_session: AsyncSession) -> AsyncIterator[None]:
    """Record who deleted each ``crdt_docs`` row, for the length of one test."""
    statements = (
        f"CREATE TABLE {_LOG} (doc_id text, deleted_as text)",
        f"GRANT INSERT, SELECT ON {_LOG} TO PUBLIC",
        f"""
        CREATE FUNCTION {_LOG}_record() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO {_LOG} VALUES (OLD.doc_id, current_user);
            RETURN OLD;
        END $$
        """,
        f"""
        CREATE TRIGGER {_LOG}_trigger BEFORE DELETE ON crdt_docs
        FOR EACH ROW EXECUTE FUNCTION {_LOG}_record()
        """,
    )
    for statement in statements:
        await real_session.execute(text(statement))
    await real_session.commit()
    try:
        yield
    finally:
        await real_session.rollback()
        await real_session.execute(text(f"DROP TRIGGER IF EXISTS {_LOG}_trigger ON crdt_docs"))
        await real_session.execute(text(f"DROP FUNCTION IF EXISTS {_LOG}_record()"))
        await real_session.execute(text(f"DROP TABLE IF EXISTS {_LOG}"))
        await real_session.commit()


async def _live_document(db: AsyncSession, fw: FileWorld) -> None:
    db.add(
        CrdtDoc(
            org_id=fw.ref.org_id,
            doc_type="file",
            doc_id=fw.ref.doc_id,
            vv=b"",
            snapshot=b"",
            loro_format="test",
            projection={},
            seeded_from="test",
        )
    )
    await db.commit()


async def _purge(db: AsyncSession, fw: FileWorld) -> None:
    context = await build_files_context(db, acting(fw.world.owner))
    async with context.repo.transaction():
        await Trash(context.repo, context.ctx, context.clock).trash(
            NodeId(fw.node_id), if_match=(await node_of(db, fw.node_id)).etag
        )
    async with context.repo.transaction():
        await Trash(context.repo, context.ctx, context.clock).purge(NodeId(fw.node_id))


@pytest.mark.usefixtures("deletes_recorded")
@pytest.mark.parametrize("bound", [True, False], ids=["a-bound-request", "an-unbound-sweep"])
async def test_a_purge_deletes_the_live_document_under_the_role_it_was_in(
    real_session: AsyncSession, org_admin: OrgWithAdmin, bound: bool
) -> None:
    fw = await file_world(real_session, org_admin)
    await _live_document(real_session, fw)
    login = (await real_session.execute(text("SELECT session_user"))).scalar_one()
    if bound:
        bind_tenant(real_session, [fw.ref.org_id])
    try:
        await _purge(real_session, fw)
        await real_session.commit()
    finally:
        real_session.info.pop(INFO_KEY, None)

    recorded = (
        await real_session.execute(
            text(f"SELECT deleted_as FROM {_LOG} WHERE doc_id = :doc"),
            {"doc": fw.ref.doc_id},
        )
    ).scalars()
    assert list(recorded) == [TENANT_ROLE if bound else login]
    left = await real_session.scalar(
        text("SELECT count(*) FROM crdt_docs WHERE doc_type = 'file' AND doc_id = :doc"),
        {"doc": fw.ref.doc_id},
    )
    assert left == 0

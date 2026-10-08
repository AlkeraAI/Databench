"""The platform-role window and the chat listing's Files read never replace the
database error they are unwinding.

During the Sep 29 2026 staging stall the ``authz.decision`` row a lease
heartbeat writes timed out inside :func:`as_platform`; the window's restamp then
ran on the dead transaction and raised ``PendingRollbackError`` — the heartbeat
answered 500 instead of the coded, retryable 503 the timeout maps to. The chat
listing's Files read (``shared_object_roles``) failed the same way with
``InFailedSQLTransactionError`` from its ``SET LOCAL ROLE NONE``. Driven with
real failures on a real Postgres.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _files_kit import FilesOrgFixture
from alkera_core.files.repo import APP_ROLE, ORG_SETTING
from alkera_core.models import EventOutbox
from backend.api.deps.files import as_platform
from backend.services.sharing.node_role import shared_object_roles
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

QUERY_CANCELED = "57014"
NOT_NULL_VIOLATION = "23502"
INSUFFICIENT_PRIVILEGE = "42501"


def _sqlstate(error: BaseException) -> str:
    return str(getattr(getattr(error, "orig", None), "sqlstate", ""))


async def _as_files_tenant(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.rollback()
    await session.begin()
    await session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    await session.execute(
        text("SELECT set_config(:name, :org, true)"), {"name": ORG_SETTING, "org": str(org_id)}
    )


async def test_a_statement_timeout_in_the_platform_window_surfaces_as_itself(
    real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    await _as_files_tenant(real_session, files_org.org.org_id)
    with pytest.raises(DBAPIError) as caught:
        async with as_platform(real_session):
            await real_session.execute(text("SET LOCAL statement_timeout = '50ms'"))
            await real_session.execute(text("SELECT pg_sleep(2)"))
    assert _sqlstate(caught.value) == QUERY_CANCELED, repr(caught.value)
    await real_session.rollback()


async def test_a_failed_decision_row_flush_surfaces_as_itself(
    real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    """The heartbeat's shape: the window's body flushes a row and the flush
    fails. SQLAlchemy then refuses every statement on the session until it is
    rolled back, which is what the restamp used to hit."""
    await _as_files_tenant(real_session, files_org.org.org_id)
    with pytest.raises(IntegrityError) as caught:
        async with as_platform(real_session):
            real_session.add(EventOutbox(org_id=files_org.org.org_id))  # no type/entity
            await real_session.flush()
    assert _sqlstate(caught.value) == NOT_NULL_VIOLATION, repr(caught.value)
    await real_session.rollback()


@pytest.mark.skipif(
    bool(os.environ.get("ALKERA_TEST_APP_LOGIN")),
    reason="revoking a grant takes the table's owner; the non-superuser tier's login is not",
)
async def test_a_failed_files_read_in_the_chat_listing_surfaces_as_itself(
    real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    """The listing's read steps into the Files role and hands it back. A read
    that fails must reach the caller as its own error, not as the 25P02 the
    hand-back answers on the aborted transaction. The grant is revoked inside
    the test's transaction, so the rollback restores it."""
    await real_session.rollback()
    await real_session.begin()
    await real_session.execute(text(f"REVOKE SELECT ON file_nodes FROM {APP_ROLE}"))
    with pytest.raises(DBAPIError) as caught:
        await shared_object_roles(
            real_session,
            org_id=files_org.org.org_id,
            object_ids=[uuid.uuid4()],
            user_id=uuid.uuid4(),
            team_ids=frozenset(),
        )
    assert _sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE, repr(caught.value)
    await real_session.rollback()

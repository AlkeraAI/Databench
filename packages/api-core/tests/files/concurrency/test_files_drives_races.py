"""Two sessions building the same org's drive at the same time.

This is the race the whole skeleton is shaped around: a member's first request
and a membership hook can both call ``ensure_org_drive`` for an org that has no
drive yet. Both run here, on real connections, forced through each other by a
checkpoint rather than by a sleep — and the org must end up with one drive, one
root and one ``/Shared``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import drives
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.ids import OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesOrg

pytestmark = pytest.mark.asyncio


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _store(session: AsyncSession) -> uuid.UUID:
    store = FileStore(
        id=uuid.uuid4(),
        driver="filesystem",
        bucket="",
        endpoint=f"/tmp/files-test/{uuid.uuid4().hex}",
        region="",
        capabilities={},
        transfer_modes=["single"],
    )
    session.add(store)
    await session.commit()
    return store.id


async def _count(session: AsyncSession, sql: str, params: dict[str, Any]) -> int:
    return int((await session.execute(text(sql), params)).scalar_one())


async def test_two_sessions_racing_ensure_org_drive_make_one_drive(
    files_session: AsyncSession,
    files_org: FilesOrg,
    sessions: Callable[..., Awaitable[list[Any]]],
    checkpoints: PausingCheckpoints,
) -> None:
    """The race the skeleton exists to survive.

    Both sessions are real connections and both run the real function. The first
    is parked between its dedup-domain insert and its drive insert; the second
    is started there, so the two are inside the window at the same time. Exactly
    one drive, one root and one ``/Shared`` must exist afterwards, and both
    callers must be handed the same drive.
    """
    store_id = await _store(files_session)
    first_session, second_session = await sessions(2)
    scope = OrgScope(org_team_id=files_org.org_team_id)
    first_repo = FilesRepo(first_session, scope)
    second_repo = FilesRepo(second_session, scope)
    ctx = _ctx(files_org)

    async def build(repo: FilesRepo, cp: Any) -> uuid.UUID:
        async with repo.transaction():
            drive = await drives.ensure_org_drive(
                repo, ctx, files_org.org_team_id, store_id=store_id, checkpoints=cp
            )
            return drive.id

    checkpoints.pause("drives.after_domain_insert")
    first = asyncio.create_task(build(first_repo, checkpoints))
    await checkpoints.wait_paused("drives.after_domain_insert")
    second = asyncio.create_task(build(second_repo, None))
    # Let the second reach its own inserts and block on the first's index
    # entries rather than racing past them.
    await asyncio.sleep(0)
    checkpoints.release("drives.after_domain_insert")
    first_id, second_id = await asyncio.gather(first, second)

    assert first_id == second_id, "both callers must be handed the one drive"
    assert (
        await _count(
            files_session,
            "SELECT count(*) FROM file_drives WHERE org_team_id = :o",
            {"o": files_org.org_team_id},
        )
        == 1
    )
    assert (
        await _count(
            files_session,
            "SELECT count(*) FROM file_nodes WHERE drive_id = :d AND parent_id IS NULL",
            {"d": first_id},
        )
        == 1
    ), "one root"
    assert (
        await _count(
            files_session,
            "SELECT count(*) FROM file_nodes WHERE drive_id = :d AND name = :n",
            {"d": first_id, "n": drives.SHARED_NAME},
        )
        == 1
    ), "one Shared"

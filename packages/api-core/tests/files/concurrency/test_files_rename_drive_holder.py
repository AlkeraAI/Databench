"""A rename outside every lease does not queue behind the drive row.

The org drive's row is the gate every tree report, upload and create in the org
takes first, and a box holding thirty chat folders keeps one of those open most
of the time. A rename used to take it too, so renaming a folder in somebody's
Home waited behind the box's batches until ``lock_timeout`` answered 503 -- and
a rename onto a taken name, which only has to look, waited just as long before
it could say so.

The holder here is a second real session with an uncommitted update on the
drive row, which is what a running tree report looks like to everyone else. The
renaming session runs with a short ``lock_timeout``, so "would have waited" is
a raised ``55P03`` rather than a hang or a sleep.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.clock import SystemClock
from alkera_core.files.errors import Conflict
from alkera_core.files.ids import NodeId, OrgScope
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from tests.files._kit.factory import FilesFactory, FilesOrg

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


@pytest.fixture
async def rig(
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> Any:
    org = await files_org_factory()
    drive = await files_factory.drive(org=org)
    tree = await files_factory.tree("home/ home/lvl1/ home/.hidden/ home/other/", drive=drive)
    holder, renamer = await sessions(2)
    await holder.execute(
        text("UPDATE file_drives SET next_ino = next_ino + 1 WHERE id = :d"), {"d": drive.id}
    )
    repo = FilesRepo(renamer, OrgScope(org_team_id=org.org_team_id))
    namespace = Namespace(repo, _ctx(org), SystemClock(), None)
    try:
        yield tree, repo, namespace, renamer
    finally:
        await holder.rollback()


async def _names(session: Any, parent_id: Any) -> set[bytes]:
    rows = await session.execute(
        text("SELECT name FROM file_nodes WHERE parent_id = :p AND trashed_at IS NULL"),
        {"p": parent_id},
    )
    return {bytes(row[0]) for row in rows.all()}


async def test_a_rename_onto_a_taken_name_is_refused_without_waiting(rig: Any) -> None:
    tree, repo, namespace, renamer = rig
    hidden = tree["home/.hidden"]
    with pytest.raises(Conflict) as refused:
        async with repo.transaction():
            await renamer.execute(text("SET LOCAL lock_timeout = '300ms'"))
            await namespace.rename(NodeId(hidden.id), b"lvl1", if_match=int(hidden.etag))
    assert refused.value.code == "files.exists"
    assert await _names(renamer, tree["home"].id) == {b"lvl1", b".hidden", b"other"}


@pytest.mark.parametrize(
    "new_name",
    [
        pytest.param(b"lvl1-renamed", id="plain"),
        pytest.param("données-renamed".encode(), id="nfc"),
        pytest.param(unicodedata.normalize("NFD", "données-renamed").encode(), id="nfd"),
        pytest.param(b"cd:renamed", id="colon"),
        pytest.param(b"a\\b", id="backslash"),
    ],
)
async def test_a_rename_outside_every_lease_lands_while_the_drive_is_held(
    rig: Any, new_name: bytes
) -> None:
    tree, repo, namespace, renamer = rig
    lvl1 = tree["home/lvl1"]
    async with repo.transaction():
        await renamer.execute(text("SET LOCAL lock_timeout = '300ms'"))
        renamed = await namespace.rename(NodeId(lvl1.id), new_name, if_match=int(lvl1.etag))
        landed = bytes(renamed.name)
    assert landed == new_name
    assert await _names(renamer, tree["home"].id) == {new_name, b".hidden", b"other"}


async def test_a_rename_to_its_own_name_is_not_a_conflict(rig: Any) -> None:
    """The look excludes the node itself: renaming onto the name it already
    has is no collision."""
    tree, repo, namespace, renamer = rig
    lvl1 = tree["home/lvl1"]
    async with repo.transaction():
        await renamer.execute(text("SET LOCAL lock_timeout = '300ms'"))
        renamed = await namespace.rename(NodeId(lvl1.id), b"lvl1", if_match=int(lvl1.etag))
        landed = bytes(renamed.name)
    assert landed == b"lvl1"

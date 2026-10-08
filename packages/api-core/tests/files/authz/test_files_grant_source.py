"""``FilesGrantSource`` against real rows: origins, and cache vs chain.

The point of the last test is the one that costs money to get wrong: the
interned ACL body is a cache, and while a node is ``acl_rewriting`` it is known
stale. A source that trusted it there would keep serving a grant that was
revoked seconds ago.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.files.authz.grants import (
    ACL_REWRITING,
    ORIGIN_DIRECT,
    ORIGIN_DRIVE_DEFAULT,
    ORIGIN_INHERITED,
    FilesGrantSource,
)
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.acl import FileAcl
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg


async def test_direct_and_inherited_grants_carry_the_granting_ancestor(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    folder, node = made["a"], made["a/b.txt"]
    await files_factory.grant(node, "user", files_org.member_id, "writer")
    await files_factory.grant(folder, "team", files_org.org_team_id, "reader")

    async with repo.transaction() as scoped:
        chain = list(await scoped.chain(node))
        grants = await FilesGrantSource().grants_for(scoped, node, chain)

    by_role = {grant.role: grant for grant in grants}
    assert by_role["writer"].origin.kind == ORIGIN_DIRECT
    assert by_role["writer"].origin.ancestor_id is None
    assert by_role["reader"].origin.kind == ORIGIN_INHERITED
    assert by_role["reader"].origin.ancestor_id == folder.id


async def test_a_revoked_grant_is_not_served(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]
    share = await files_factory.grant(node, "user", files_org.member_id, "owner")

    async with repo.transaction() as scoped:
        chain = list(await scoped.chain(node))
        before = await FilesGrantSource().grants_for(scoped, node, chain)

    share.revoked_at = share.created_at
    await files_session.commit()

    async with repo.transaction() as scoped:
        after = await FilesGrantSource().grants_for(scoped, node, chain)

    assert [grant.role for grant in before] == ["owner"]
    assert list(after) == []


async def test_the_interned_body_is_served_when_the_cache_is_trustworthy(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]
    acl = FileAcl(
        id=uuid.uuid4(),
        org_team_id=files_org.org_team_id,
        body=[
            {
                "principal_kind": "user",
                "principal_id": str(files_org.member_id),
                "role": "manager",
                "origin": ORIGIN_DRIVE_DEFAULT,
            }
        ],
        body_hash=uuid.uuid4().hex * 2,
    )
    files_session.add(acl)
    node.acl_id = acl.id
    await files_session.commit()

    async with repo.transaction() as scoped:
        chain = list(await scoped.chain(node))
        grants = await FilesGrantSource().grants_for(scoped, node, chain)

    assert [(g.principal.kind, g.principal.id, g.role) for g in grants] == [
        ("user", files_org.member_id, "manager")
    ]
    assert grants[0].origin.kind == ORIGIN_DRIVE_DEFAULT


async def test_a_rewriting_node_recomputes_from_the_chain_and_ignores_the_stale_cache(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    folder, node = made["a"], made["a/b.txt"]
    await files_factory.grant(folder, "user", files_org.member_id, "reader")
    stale = FileAcl(
        id=uuid.uuid4(),
        org_team_id=files_org.org_team_id,
        body=[
            {
                "principal_kind": "user",
                "principal_id": str(files_org.member_id),
                # The grant the chain no longer carries: if the cache won, an
                # owner would be served where the truth says reader.
                "role": "owner",
                "origin": ORIGIN_DIRECT,
            }
        ],
        body_hash=uuid.uuid4().hex * 2,
    )
    files_session.add(stale)
    node.acl_id = stale.id
    await files_session.commit()

    async with repo.transaction() as scoped:
        chain = list(await scoped.chain(node))
        trusted = await FilesGrantSource().grants_for(scoped, node, chain)

    node.state = ACL_REWRITING
    await files_session.commit()

    async with repo.transaction() as scoped:
        rewriting = await FilesGrantSource().grants_for(scoped, node, chain)

    # Before: the cache is believed, stale role and all.
    assert [grant.role for grant in trusted] == ["owner"]
    # While rewriting: the chain wins.
    assert [grant.role for grant in rewriting] == ["reader"]
    assert rewriting[0].origin.ancestor_id == folder.id


async def test_a_node_with_no_cache_falls_back_to_the_chain(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    await files_factory.grant(made["a"], "user", files_org.member_id, "commenter")
    node = made["a/b.txt"]

    async with repo.transaction() as scoped:
        chain = list(await scoped.chain(node))
        grants = await FilesGrantSource().grants_for(scoped, node, chain)

    assert node.acl_id is None
    assert [grant.role for grant in grants] == ["commenter"]


@pytest.mark.parametrize(
    "ace",
    [
        pytest.param({"principal_kind": "user", "role": "owner"}, id="no-principal-id"),
        pytest.param(
            {"principal_kind": "user", "principal_id": "not-a-uuid", "role": "owner"},
            id="unparseable-id",
        ),
        pytest.param(
            {"principal_id": "00000000-0000-4000-8000-000000000001", "role": "owner"},
            id="no-kind",
        ),
    ],
)
async def test_an_unreadable_ace_is_dropped_rather_than_guessed_at(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    ace: dict[str, str],
) -> None:
    """A newer writer's shape must never be turned into a grant by defaulting a
    missing field — an unreadable entry allows nothing."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]
    acl = FileAcl(
        id=uuid.uuid4(),
        org_team_id=files_org.org_team_id,
        body=[ace],
        body_hash=uuid.uuid4().hex * 2,
    )
    files_session.add(acl)
    node.acl_id = acl.id
    await files_session.commit()

    async with repo.transaction() as scoped:
        chain = list(await scoped.chain(node))
        grants = await FilesGrantSource().grants_for(scoped, node, chain)

    assert list(grants) == []

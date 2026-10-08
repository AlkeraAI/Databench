"""ACL interning, grants, revokes and the background rewrite.

The property under test everywhere below is one sentence: **a node's cached
``acl_id`` names exactly the union of its chain**. Every test either builds that
union and checks the cache matches it, or breaks the cache on purpose and checks
a reader still gets the chain's answer.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.principal import Principal as ActingPrincipal
from alkera_core.config import settings
from alkera_core.files import acl
from alkera_core.files.authz.defaults import ORG_POLICY_RESTRICTED, DriveFolder, default_acl
from alkera_core.files.authz.grants import (
    ACL_REWRITING,
    ORIGIN_DIRECT,
    ORIGIN_DRIVE_DEFAULT,
    ORIGIN_INHERITED,
    FilesGrantSource,
    Grant,
    GrantOrigin,
    Principal,
    register_principal_kind,
    unregister_principal_kind,
)
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.errors import InvalidRequest, NotFound, PreconditionFailed
from alkera_core.files.ids import AclId, NodeId, OperationId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.acl import FileAcl, FileAclMember, FileShare
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.tree import FileNode
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=ActingPrincipal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _user(id: uuid.UUID) -> Principal:
    return Principal(kind="user", id=id)


async def _node(session: AsyncSession, node_id: uuid.UUID) -> FileNode:
    """Re-read a node from the database, never from an identity map."""
    got = await session.get(FileNode, node_id, populate_existing=True)
    assert got is not None
    await session.refresh(got)
    return got


async def _chain_union(repo: FilesRepo, node: FileNode) -> acl.AclBody:
    """The truth, recomputed from scratch: what the cache must equal."""
    return await acl.effective_body(repo, node, await repo.chain(node))


async def _drain(repo: FilesRepo, op_id: OperationId, *, batch: int = 2) -> int:
    """Run the rewrite to completion the way a worker would, batch by batch."""
    total = 0
    for _ in range(50):
        async with repo.transaction():
            moved = await acl.rewrite(repo, op_id, batch=batch)
        total += moved
        if moved == 0:
            break
    return total


# ---- interning -----------------------------------------------------------


async def test_interning_the_same_body_twice_creates_one_row_and_its_members(
    repo: FilesRepo, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """Interning is insert-or-read: the second call reuses the first row.

    Fails if ``intern`` ever becomes an unconditional INSERT (two rows) or an
    ``ON CONFLICT DO UPDATE`` (the members would be written twice).
    """
    body = acl.AclBody.from_grants(
        [
            Grant(principal=_user(files_org.admin_id), role="owner"),
            Grant(principal=_user(files_org.member_id), role="reader"),
        ]
    )

    async with repo.transaction():
        first = await acl.intern(repo, body)
    async with repo.transaction():
        second = await acl.intern(repo, body)

    assert first is not None
    assert first == second

    rows = await files_session.execute(
        select(func.count())
        .select_from(FileAcl)
        .where(FileAcl.org_team_id == files_org.org_team_id)
    )
    assert rows.scalar_one() == 1

    members = (
        await files_session.execute(
            select(FileAclMember.principal_id, FileAclMember.max_role).where(
                FileAclMember.acl_id == first
            )
        )
    ).all()
    assert {(pid, role) for pid, role in members} == {
        (files_org.admin_id, "owner"),
        (files_org.member_id, "reader"),
    }


async def test_the_members_table_carries_the_strongest_role_per_principal(
    repo: FilesRepo, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """Two rungs for one principal explode to one row at the higher rung.

    The negative twin of the test above: if ``members()`` kept insertion order
    instead of comparing the ladder, this asserts ``reader`` and fails.
    """
    body = acl.AclBody.from_grants(
        [
            Grant(principal=_user(files_org.member_id), role="reader"),
            Grant(
                principal=_user(files_org.member_id),
                role="manager",
                origin=GrantOrigin.inherited(uuid.uuid4()),
            ),
        ]
    )
    async with repo.transaction():
        acl_id = await acl.intern(repo, body)

    members = (
        (
            await files_session.execute(
                select(FileAclMember.max_role).where(FileAclMember.acl_id == acl_id)
            )
        )
        .scalars()
        .all()
    )
    assert list(members) == ["manager"]


async def test_two_orgs_interning_the_same_grants_get_their_own_rows(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_org_factory: Any,
    files_session: AsyncSession,
) -> None:
    """The tenant is folded into the hash, so an RLS-invisible row is never hit.

    Without the fold the second org's ``ON CONFLICT DO NOTHING`` collides with a
    row its own scoped SELECT cannot see, and ``intern`` returns nothing at all.
    """
    other = await files_org_factory()
    body = acl.AclBody.from_grants([Grant(principal=_user(files_org.admin_id), role="reader")])

    async with repo.transaction():
        mine = await acl.intern(repo, body)
    other_repo = FilesRepo(files_session, OrgScope(org_team_id=other.org_team_id))
    async with other_repo.transaction():
        theirs = await acl.intern(other_repo, body)

    assert mine is not None
    assert theirs is not None
    assert mine != theirs


async def test_a_body_with_an_expiry_is_not_interned(
    repo: FilesRepo, files_org: FilesOrg, clock: Any
) -> None:
    """A time-boxed grant keeps the node on the chain instead of a lying cache.

    JSONB round-trips a ``datetime`` to a string and the cache reader drops it,
    so an interned expiring grant would read back as permanent.
    """
    body = acl.AclBody.from_grants(
        [
            Grant(
                principal=_user(files_org.member_id),
                role="reader",
                expires_at=clock.now(),
            )
        ]
    )
    assert not body.internable
    async with repo.transaction():
        assert await acl.intern(repo, body) is None


# ---- grant, inherit, revoke ---------------------------------------------


async def test_a_grant_at_an_ancestor_reaches_a_descendant_as_an_inherited_ace(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """Trickle-down is structural: the descendant's body names the ancestor."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b/ a/b/c.txt", drive=drive)
    top, leaf = made["a"], made["a/b/c.txt"]
    ctx = _ctx(files_org)

    async with repo.transaction():
        await acl.grant(repo, ctx, top, _user(files_org.member_id), "writer")

    async with repo.transaction():
        fresh_leaf = await _node(files_session, leaf.id)
        body = await _chain_union(repo, fresh_leaf)

    inherited = [ace for ace in body.aces if ace["origin"] == ORIGIN_INHERITED]
    assert [ace["principal_id"] for ace in inherited] == [str(files_org.member_id)]
    assert inherited[0]["origin_ancestor_id"] == str(top.id)
    assert inherited[0]["role"] == "writer"


async def test_a_grant_on_the_node_itself_is_direct_not_inherited(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The negative twin: the same grant one level down is ``direct``."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    leaf = made["a/b.txt"]

    async with repo.transaction():
        await acl.grant(repo, _ctx(files_org), leaf, _user(files_org.member_id), "writer")

    async with repo.transaction():
        body = await _chain_union(repo, await _node(files_session, leaf.id))

    assert [ace["origin"] for ace in body.aces] == [ORIGIN_DIRECT]
    assert "origin_ancestor_id" not in body.aces[0]


async def _home_of(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, spec: str
) -> dict[str, FileNode]:
    """``home/<member>/`` and whatever ``spec`` hangs under it, with the home
    carrying the drive default a membership mints: ``{user: owner}`` interned
    onto its ``acl_id`` and nowhere else — no ``file_shares`` row anywhere."""
    drive = await files_factory.drive()
    made = await files_factory.tree(f"home/ home/dana/ {spec}", drive=drive)
    async with repo.transaction():
        minted = await acl.intern(
            repo,
            acl.AclBody.from_grants(
                default_acl(
                    DriveFolder.HOME,
                    org_policy=ORG_POLICY_RESTRICTED,
                    org_team_id=files_org.org_team_id,
                    subject_id=files_org.member_id,
                )
            ),
        )
        await repo.session.execute(
            repo.update_nodes().where(FileNode.id == made["home/dana"].id).values(acl_id=minted)
        )
    return made


def _owner_aces(body: list[Any], owner_id: uuid.UUID) -> list[dict[str, Any]]:
    return [ace for ace in body if ace["principal_id"] == str(owner_id) and ace["role"] == "owner"]


async def test_a_grant_under_a_home_keeps_the_homes_owner_in_every_cache_it_writes(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The home's owner grant is minted into the home's body only, so a cache
    rebuilt from the chain has to read it from there — at the granted node in
    the granting transaction, and at every descendant the rewrite repairs.
    Without that the owner's own share of a folder in their home writes them
    out of it: a body naming only the grantee, trusted over the chain."""
    made = await _home_of(repo, files_factory, files_org, "home/dana/kept/ home/dana/kept/deep.txt")
    home, kept, deep = made["home/dana"], made["home/dana/kept"], made["home/dana/kept/deep.txt"]

    async with repo.transaction():
        await acl.grant(repo, _ctx(files_org), kept, _user(files_org.admin_id), "writer")
        op_id = OperationId(
            (await repo.execute_scoped(select(FileOp.id).order_by(FileOp.created_at.desc())))
            .scalars()
            .first()
        )
    async with repo.transaction():
        at_kept = await repo.acl(AclId((await _node(files_session, kept.id)).acl_id))
    assert at_kept is not None
    owner = _owner_aces(at_kept.body, files_org.member_id)
    assert [(ace["origin"], ace["origin_ancestor_id"]) for ace in owner] == [
        (ORIGIN_INHERITED, str(home.id))
    ]
    assert {ace["principal_id"] for ace in at_kept.body} == {
        str(files_org.member_id),
        str(files_org.admin_id),
    }

    assert await _drain(repo, op_id) >= 1
    async with repo.transaction():
        fresh = await _node(files_session, deep.id)
        at_deep = await repo.acl(AclId(fresh.acl_id)) if fresh.acl_id else None
    assert fresh.state == "live"
    assert at_deep is not None
    assert [
        (ace["origin"], ace["origin_ancestor_id"])
        for ace in _owner_aces(at_deep.body, files_org.member_id)
    ] == [(ORIGIN_INHERITED, str(home.id))]


async def test_re_interning_a_home_with_no_shares_is_a_fixpoint(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The chain union of the home itself is the body it was minted with — the
    owner ACE keeps its ``drive_default`` origin rather than turning into a
    ``direct`` grant that no ``file_shares`` row backs — so a grant made on the
    home re-interns onto the same row instead of forking a near-twin."""
    made = await _home_of(repo, files_factory, files_org, "home/dana/kept/")
    home = made["home/dana"]

    async with repo.transaction():
        fresh = await _node(files_session, home.id)
        minted = await repo.acl(AclId(fresh.acl_id))
        union = await _chain_union(repo, fresh)
    assert minted is not None
    assert union.as_json() == minted.body
    assert [ace["origin"] for ace in union.aces] == [ORIGIN_DRIVE_DEFAULT]
    assert union.hash_for(files_org.org_team_id) == minted.body_hash


async def test_revoking_an_inherited_grant_at_the_child_names_the_ancestor(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """409 ``files.inherited_grant``, with the granting node in ``detail``.

    Files has no deny entries, so shadowing the grant here would be a lie; the
    only honest answer points the caller at where it can actually be removed.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    top, leaf = made["a"], made["a/b.txt"]
    ctx = _ctx(files_org)

    async with repo.transaction():
        share = await acl.grant(repo, ctx, top, _user(files_org.member_id), "writer")

    # Captured before the refusal: the rolled-back transaction expires every
    # instance, so reading these afterwards would be a lazy load.
    top_id, share_id = top.id, share.id

    with pytest.raises(acl.InheritedGrant) as caught:
        async with repo.transaction():
            await acl.revoke(repo, ctx, await _node(files_session, leaf.id), share_id)

    assert caught.value.code == "files.inherited_grant"
    assert caught.value.detail["granting_node_id"] == str(top_id)
    assert caught.value.detail["share_id"] == str(share_id)


async def test_revoking_at_the_ancestor_clears_the_grant_below_after_the_rewrite(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The whole grant-then-revoke round trip, proven at the descendant's cache.

    Fails if ``revoke`` forgets to re-mark the subtree: the leaf would keep the
    ``acl_id`` interned while the grant was live, and its cache would still
    name a principal with no surviving share.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b/ a/b/c.txt", drive=drive)
    top, leaf = made["a"], made["a/b/c.txt"]
    ctx = _ctx(files_org)

    async with repo.transaction():
        share = await acl.grant(repo, ctx, top, _user(files_org.member_id), "writer")
        granted_op = OperationId(
            (await repo.execute_scoped(select(FileOp.id).order_by(FileOp.created_at.desc())))
            .scalars()
            .first()
        )
    await _drain(repo, granted_op)

    async with repo.transaction():
        cached = await repo.acl(AclId((await _node(files_session, leaf.id)).acl_id))
    assert cached is not None
    assert str(files_org.member_id) in {ace["principal_id"] for ace in cached.body}

    async with repo.transaction():
        await acl.revoke(repo, ctx, await _node(files_session, top.id), share.id)
        revoke_op = OperationId(
            (await repo.execute_scoped(select(FileOp.id).order_by(FileOp.created_at.desc())))
            .scalars()
            .first()
        )
    await _drain(repo, revoke_op)

    async with repo.transaction():
        fresh = await _node(files_session, leaf.id)
        after = await repo.acl(AclId(fresh.acl_id)) if fresh.acl_id else None
    assert fresh.state == "live"
    body = [] if after is None else after.body
    assert str(files_org.member_id) not in {ace["principal_id"] for ace in body}


@pytest.mark.parametrize(
    ("kind", "which"),
    [
        pytest.param("user", "foreign", id="user-in-another-org"),
        pytest.param("user", "absent", id="user-that-does-not-exist"),
        pytest.param("team", "foreign", id="team-in-another-org"),
    ],
)
async def test_a_grant_to_a_foreign_principal_is_refused_by_the_service(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_org_factory: Any,
    files_session: AsyncSession,
    kind: str,
    which: str,
) -> None:
    """The service refuses before the trigger, and refuses as *not found*.

    A caller who may not grant to a principal may not learn it exists either,
    so the class is ``NotFound`` — never a message that confirms the other
    tenant's row.
    """
    other = await files_org_factory()
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]
    target = {
        ("user", "foreign"): other.admin_id,
        ("user", "absent"): uuid.uuid4(),
        ("team", "foreign"): other.org_team_id,
        ("org", "foreign"): other.org_team_id,
    }[(kind, which)]

    # Captured before the refusal: the rolled-back transaction expires every
    # instance, so reading ``node.id`` afterwards would be a lazy load.
    node_id = node.id

    with pytest.raises(NotFound):
        async with repo.transaction():
            await acl.grant(repo, _ctx(files_org), node, Principal(kind=kind, id=target), "reader")

    remaining = await files_session.execute(
        select(func.count()).select_from(FileShare).where(FileShare.node_id == node_id)
    )
    assert remaining.scalar_one() == 0


async def test_a_grant_to_a_principal_in_this_org_is_accepted(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """The positive twin, so the refusal above is not simply "everything fails"."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]
    async with repo.transaction():
        share = await acl.grant(
            repo, _ctx(files_org), node, Principal(kind="org", id=files_org.org_team_id), "reader"
        )
    assert share.principal_id == files_org.org_team_id


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("link", id="link"),
        pytest.param("service", id="a-kind-nobody-registered"),
        pytest.param("User", id="a-registered-kind-in-the-wrong-case"),
        pytest.param("", id="no-kind-at-all"),
    ],
)
async def test_a_grant_naming_an_unregistered_principal_kind_is_refused(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    kind: str,
) -> None:
    """Only a kind the decider has a matcher for may become a row.

    Anything else is a grant no reader can ever resolve, and the database
    trigger would refuse it with a raw integrity error rather than a typed one,
    so the service refuses it first — as an invalid request, because the kind is
    the caller's own input and names nothing it may not know.
    """
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]
    node_id = node.id

    with pytest.raises(InvalidRequest) as caught:
        async with repo.transaction():
            await acl.grant(
                repo,
                _ctx(files_org),
                node,
                Principal(kind=kind, id=files_org.member_id),
                "reader",
            )
    assert caught.value.code == "files.unknown_principal_kind"

    remaining = await files_session.execute(
        select(func.count()).select_from(FileShare).where(FileShare.node_id == node_id)
    )
    assert remaining.scalar_one() == 0


async def test_the_refusal_reads_the_registry_rather_than_a_list_spelled_here(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """Teaching the decider a kind is all it takes to get past this gate: a kind
    registered at runtime is refused for who it names, not for what it is.
    """
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]
    register_principal_kind("scratch_kind", lambda principal, caller: False)
    try:
        with pytest.raises(NotFound):
            async with repo.transaction():
                await acl.grant(
                    repo,
                    _ctx(files_org),
                    node,
                    Principal(kind="scratch_kind", id=files_org.member_id),
                    "reader",
                )
    finally:
        unregister_principal_kind("scratch_kind")


# ---- the rewrite ---------------------------------------------------------


async def test_the_rewrite_leaves_every_node_equal_to_its_chain_union(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """Every node equals its chain union, checked against a from-scratch recomputation."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b/ a/b/c/ a/b/c/d.txt a/b/e.txt a/f.txt", drive=drive)
    ctx = _ctx(files_org)

    async with repo.transaction():
        await acl.grant(repo, ctx, made["a"], _user(files_org.member_id), "reader")
        op_id = OperationId(
            (await repo.execute_scoped(select(FileOp.id).order_by(FileOp.created_at.desc())))
            .scalars()
            .first()
        )
    await _drain(repo, op_id)

    async with repo.transaction():
        for node in made.values():
            fresh = await _node(files_session, node.id)
            assert fresh.state == "live", f"{fresh.id} was left marked"
            expected = await _chain_union(repo, fresh)
            cached = await repo.acl(AclId(fresh.acl_id))
            assert cached is not None
            assert cached.body == expected.as_json(), f"{fresh.id} cache != chain union"


async def test_a_killed_rewrite_resumes_from_its_cursor(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """A crash mid-rewrite loses one batch and no more.

    The kill lands *after* the first batch committed, so resuming must repair
    only what is left. Fails if the cursor is written before the repairs it
    describes, or if the state flag and the ``acl_id`` are set in two statements.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt a/c.txt a/d.txt a/e.txt", drive=drive)
    ctx = _ctx(files_org)

    async with repo.transaction():
        await acl.grant(repo, ctx, made["a"], _user(files_org.member_id), "reader")
        op_id = OperationId(
            (await repo.execute_scoped(select(FileOp.id).order_by(FileOp.created_at.desc())))
            .scalars()
            .first()
        )

    async with repo.transaction():
        first = await acl.rewrite(repo, op_id, batch=2)
    assert first == 2

    still_marked = (
        await files_session.execute(
            select(func.count())
            .select_from(FileNode)
            .where(FileNode.drive_id == drive.id, FileNode.state == ACL_REWRITING)
        )
    ).scalar_one()
    assert still_marked == 2

    resumed = await _drain(repo, op_id, batch=2)
    assert resumed == 2

    async with repo.transaction():
        for node in made.values():
            fresh = await _node(files_session, node.id)
            assert fresh.state == "live"
            expected = await _chain_union(repo, fresh)
            cached = await repo.acl(AclId(fresh.acl_id))
            assert cached is not None
            assert cached.body == expected.as_json()


async def test_a_marked_descendant_reads_its_role_from_the_chain_not_the_cache(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The reason the mark is safe: while marked, the cache is bypassed.

    Fails if ``_mark_subtree_rewriting`` is dropped — the leaf would keep its
    pre-grant ``acl_id`` and the grant source would answer from it.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    top, leaf = made["a"], made["a/b.txt"]
    ctx = _ctx(files_org)

    async with repo.transaction():
        await acl.grant(repo, ctx, top, _user(files_org.member_id), "writer")

    async with repo.transaction():
        fresh = await _node(files_session, leaf.id)
        assert fresh.state == ACL_REWRITING
        grants = await FilesGrantSource().grants_for(repo, fresh, await repo.chain(fresh))

    assert {(g.principal.id, g.role) for g in grants} == {(files_org.member_id, "writer")}


# ---- the forced interleaving --------------------------------------------


@pytest.fixture
async def two_backends() -> Any:
    """``await two_backends(n)`` → n sessions, each on its own connection.

    The same shape as the ``sessions`` fixture in ``packages/api-core/tests/files/concurrency/``,
    rebuilt here because that conftest is scoped to its own directory. One
    shared session would prove nothing: SQLAlchemy would serialize the
    statements onto a single connection and the interleaving under test could
    never happen. ``max_overflow=0`` makes an unaccounted-for checkout block
    rather than quietly opening a connection the test did not plan for.
    """
    engines: list[Any] = []
    connections: list[Any] = []
    opened: list[AsyncSession] = []

    async def factory(n: int) -> list[AsyncSession]:
        engine = create_async_engine(settings.database_url, pool_size=n, max_overflow=0)
        engines.append(engine)
        made: list[AsyncSession] = []
        for _ in range(n):
            connection = await engine.connect()
            connections.append(connection)
            session = AsyncSession(bind=connection, expire_on_commit=False)
            opened.append(session)
            made.append(session)
        return made

    yield factory

    for session in opened:
        await session.close()
    for connection in connections:
        await connection.close()
    for engine in engines:
        await engine.dispose()


async def test_a_grant_below_a_paused_rewrite_is_not_lost_when_it_resumes(
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    two_backends: Any,
    checkpoints: PausingCheckpoints,
) -> None:
    """Two real backends, one forced order, nothing lost.

    The rewrite for a grant at ``a`` is parked at ``acl.rewrite_before_node``
    with the leaf still marked. A second session grants directly on that leaf,
    which re-marks it and enqueues its own repair. When the parked rewrite
    resumes it must NOT write the body it computed before that grant existed —
    the compare-and-swap on ``state`` is what refuses it — and the final cache
    must equal the chain union, which now includes both grants.

    Fails if the clearing UPDATE drops its ``state == 'acl_rewriting'``
    predicate: the stale body wins and the direct grant vanishes from the cache.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    top, leaf = made["a"], made["a/b.txt"]
    ctx = _ctx(files_org)

    outer, inner = await two_backends(2)
    slow = FilesRepo(outer, files_org.scope)
    fast = FilesRepo(inner, files_org.scope)

    async with slow.transaction():
        await acl.grant(slow, ctx, top, _user(files_org.admin_id), "manager")
        op_id = OperationId(
            (await slow.execute_scoped(select(FileOp.id).order_by(FileOp.created_at.desc())))
            .scalars()
            .first()
        )

    checkpoints.pause("acl.rewrite_before_node")

    async def parked_rewrite() -> int:
        async with slow.transaction():
            return await acl.rewrite(slow, op_id, batch=10, checkpoints=checkpoints)

    async def concurrent_grant() -> None:
        await checkpoints.wait_paused("acl.rewrite_before_node")
        async with fast.transaction():
            fresh = await fast.node(NodeId(leaf.id))
            assert fresh is not None
            await acl.grant(fast, ctx, fresh, _user(files_org.member_id), "reader")
        checkpoints.release("acl.rewrite_before_node")

    repaired, _ = await asyncio.gather(parked_rewrite(), concurrent_grant())

    # The parked batch found the leaf marked, computed a body without the
    # concurrent grant, and lost the compare-and-swap: it repaired nothing.
    assert repaired == 0

    async with slow.transaction():
        fresh = await slow.node(NodeId(leaf.id))
        assert fresh is not None
        expected = await acl.effective_body(slow, fresh, await slow.chain(fresh))
        principals = {ace["principal_id"] for ace in expected.aces}
        cached = await slow.acl(AclId(fresh.acl_id)) if fresh.acl_id else None

    assert principals == {str(files_org.admin_id), str(files_org.member_id)}
    assert cached is not None
    assert cached.body == expected.as_json()


# ---- one live share per principal ----------------------------------------


async def _live_shares(repo: FilesRepo, node_id: uuid.UUID) -> list[FileShare]:
    async with repo.transaction():
        rows = await repo.execute_scoped(
            repo.select_shares().where(FileShare.node_id == node_id, FileShare.revoked_at.is_(None))
        )
        return list(rows.scalars().all())


async def test_granting_a_second_rung_moves_the_one_live_share(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """Changing someone's role leaves them on ONE row, at the later rung.

    A second row makes the grant un-withdrawable by the panel that made it: the
    panel revokes the share id it is holding, the other row survives, and the
    union of live shares keeps the person's access.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    leaf = made["a/b.txt"]
    ctx = _ctx(files_org)

    async with repo.transaction():
        first = await acl.grant(repo, ctx, leaf, _user(files_org.member_id), "reader")
    first_id = first.id

    async with repo.transaction():
        second = await acl.grant(
            repo, ctx, await _node(files_session, leaf.id), _user(files_org.member_id), "writer"
        )
    assert second.id == first_id

    live = await _live_shares(repo, leaf.id)
    assert [(row.id, row.role) for row in live] == [(first_id, "writer")]


async def test_a_revoke_after_a_role_change_leaves_no_live_share(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The journey the share panel drives: share, change the rung, then Remove.

    Nothing of the person's access may survive the revoke — a second live row
    at the earlier rung is access the panel has already said is gone.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    leaf = made["a/b.txt"]
    ctx = _ctx(files_org)

    async with repo.transaction():
        share = await acl.grant(repo, ctx, leaf, _user(files_org.member_id), "reader")
    share_id = share.id
    async with repo.transaction():
        await acl.grant(
            repo, ctx, await _node(files_session, leaf.id), _user(files_org.member_id), "manager"
        )
    async with repo.transaction():
        await acl.revoke(repo, ctx, await _node(files_session, leaf.id), share_id)

    assert await _live_shares(repo, leaf.id) == []

    async with repo.transaction():
        body = await _chain_union(repo, await _node(files_session, leaf.id))
    assert str(files_org.member_id) not in {ace["principal_id"] for ace in body.aces}


async def test_a_grant_to_a_second_principal_keeps_its_own_share(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The negative twin: the row a grant moves is the principal's, not the
    node's. Two people sharing one file keep two rows."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    leaf = made["a/b.txt"]
    ctx = _ctx(files_org)

    async with repo.transaction():
        await acl.grant(repo, ctx, leaf, _user(files_org.member_id), "reader")
    async with repo.transaction():
        await acl.grant(
            repo, ctx, await _node(files_session, leaf.id), _user(files_org.admin_id), "writer"
        )

    live = await _live_shares(repo, leaf.id)
    assert sorted((str(row.principal_id), row.role) for row in live) == sorted(
        [(str(files_org.member_id), "reader"), (str(files_org.admin_id), "writer")]
    )


async def test_a_grant_after_a_revoke_is_a_new_share(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """A withdrawn grant is history, not a row to reopen: re-sharing with the
    same person mints a new share and leaves the revoked one as it was."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    leaf = made["a/b.txt"]
    ctx = _ctx(files_org)

    async with repo.transaction():
        first = await acl.grant(repo, ctx, leaf, _user(files_org.member_id), "reader")
    first_id = first.id
    async with repo.transaction():
        await acl.revoke(repo, ctx, await _node(files_session, leaf.id), first_id)
    async with repo.transaction():
        again = await acl.grant(
            repo, ctx, await _node(files_session, leaf.id), _user(files_org.member_id), "writer"
        )

    assert again.id != first_id
    live = await _live_shares(repo, leaf.id)
    assert [(row.id, row.role) for row in live] == [(again.id, "writer")]


async def test_a_grant_checks_its_precondition_under_the_node_lock(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
) -> None:
    """Two grants that both read etag N before either wrote must not both land.

    The route's early etag check runs before the row lock, so the grant itself
    re-checks the etag on the locked row: the one that wins moves the etag, and
    the one that waited sees the move and is refused with nothing written.
    """
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]
    node_id = node.id
    seen = int(node.etag)

    async with repo.transaction():
        first = await acl.grant(
            repo, _ctx(files_org), node, _user(files_org.member_id), "reader", if_match=seen
        )
        first_id = first.id
    with pytest.raises(PreconditionFailed):
        async with repo.transaction():
            await acl.grant(
                repo,
                _ctx(files_org),
                await _node(files_session, node_id),
                _user(files_org.member_id),
                "owner",
                if_match=seen,
            )
    stored = (
        await files_session.execute(
            select(FileShare.role).where(
                FileShare.node_id == node_id, FileShare.revoked_at.is_(None)
            )
        )
    ).scalars()
    assert list(stored) == ["reader"]

    with pytest.raises(PreconditionFailed):
        async with repo.transaction():
            await acl.revoke(
                repo, _ctx(files_org), await _node(files_session, node_id), first_id, if_match=seen
            )
    current = await _node(files_session, node_id)
    async with repo.transaction():
        revoked = await acl.revoke(
            repo, _ctx(files_org), current, first_id, if_match=int(current.etag)
        )
    assert revoked.revoked_at is not None


async def test_an_org_grant_naming_any_org_but_this_one_is_an_invalid_request(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_org_factory: Any,
    files_session: AsyncSession,
) -> None:
    """An org grant has exactly one valid id, the caller's own org, which the
    caller already knows. Any other id is the caller's input error, answered the
    same way for a real org and an id never issued, so it confirms nothing."""
    other = await files_org_factory()
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]
    node_id = node.id
    for target in (other.org_team_id, uuid.uuid4()):
        with pytest.raises(InvalidRequest) as refused:
            async with repo.transaction():
                await acl.grant(
                    repo,
                    _ctx(files_org),
                    await _node(files_session, node_id),
                    Principal(kind="org", id=target),
                    "reader",
                )
        assert refused.value.code == "files.org_principal_not_own"
    remaining = await files_session.execute(
        select(func.count()).select_from(FileShare).where(FileShare.node_id == node_id)
    )
    assert remaining.scalar_one() == 0

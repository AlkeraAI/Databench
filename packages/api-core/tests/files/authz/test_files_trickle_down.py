"""Trickle-down as a structural property, over generated trees and histories.

One sentence holds the whole permission model up: **a node is never less
accessible than its ancestor**. Files has no deny entries, so every operation
the tree admits — a create under a shared folder, a rename, a move that changes
which chain a subtree hangs from, a grant, a revoke — has to preserve it. The
property below plays generated operation sequences against the real
:mod:`alkera_core.files.acl` and :mod:`alkera_core.files.namespace` on real
Postgres and, after *every* operation, checks:

1. for every caller and every node, ``rank(effective_role(N)) >=
   rank(effective_role(A))`` for each ancestor ``A`` of ``N``;
2. every materialized ``acl_id`` body equals the chain union recomputed from
   scratch — the cache is a cache, never the truth;
3. a revoke aimed at an inherited grant is refused, naming the ancestor that
   granted it.

It is written against ``GrantSource``/``AccessDecider`` — the seams — rather
than against ``file_shares``, so the day the platform engine replaces both this
file keeps running unchanged.

The deterministic tests underneath pin the two consequences the property
observes but cannot construct on demand: revoking at the ancestor clears the
grant all the way down, and moving a node out from under an ancestor drops that
ancestor's grant across the moved subtree.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Final

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.principal import Principal as ActingPrincipal
from alkera_core.config import settings
from alkera_core.files import acl, errors, names
from alkera_core.files.authz.decider import (
    NO_DOWNLOAD_BIT,
    AccessFacts,
    LadderDecider,
    effective_role,
    flags_for_child,
)
from alkera_core.files.authz.grants import (
    ACL_REWRITING,
    FilesGrantSource,
    GrantSource,
    Principal,
)
from alkera_core.files.authz.ladder import DEFAULT_LADDER
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, NodeId, OperationId, OrgScope
from alkera_core.files.lease_tree import LeaseTreeService, TreeChange
from alkera_core.files.leases import LeaseService
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.acl import FileShare
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg, _seed_org
from tests.files.strategies import GrantSpec, grants, op_sequences, trees

#: The two callers every assertion is made about, and the principals a generated
#: grant is mapped onto. A generated ``PrincipalSpec`` carries a random uuid,
#: which no tenant contains — the in-org check would refuse it before the
#: property got to say anything — so the generator decides the *shape* of the
#: history (who, how often, in what order) and the tenant supplies real ids.
_KIND_OF: Final[Mapping[int, str]] = {0: "user", 1: "user", 2: "team"}


def _ctx(org: FilesOrg, user_id: uuid.UUID | None = None) -> ActingContext:
    return ActingContext(
        acting_principal=ActingPrincipal(
            kind=PrincipalKind.USER,
            id=str(user_id or org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _principals(org: FilesOrg) -> tuple[Principal, ...]:
    """The three real principals a generated grant can name."""
    return (
        Principal(kind="user", id=org.admin_id),
        Principal(kind="user", id=org.member_id),
        Principal(kind="team", id=org.org_team_id),
    )


def _facts(org: FilesOrg) -> AccessFacts:
    """Both callers are members of the org root team, which is what makes a
    ``team`` grant on it a candidate; neither is an org admin, so the descent
    floor cannot mask a trickle-down failure."""
    return AccessFacts(team_ids=frozenset({org.org_team_id}), org_admin=False)


async def _role_of(
    repo: FilesRepo,
    source: GrantSource,
    org: FilesOrg,
    drive: FileDrive,
    node: FileNode,
    user_id: uuid.UUID,
) -> str | None:
    chain = await repo.chain(node)
    granted = await source.grants_for(repo, node, chain)
    access = effective_role(
        _ctx(org, user_id), node, chain, granted, drive, _facts(org), decider=LadderDecider()
    )
    return access.role


async def _ace_keys(body: Sequence[Any]) -> set[tuple[tuple[str, str], ...]]:
    """A body as a comparable set: ACE dicts with every value stringified, so a
    uuid that JSONB round-tripped to text still equals the one in memory."""
    out: set[tuple[tuple[str, str], ...]] = set()
    for ace in body:
        if not isinstance(ace, dict):
            continue
        out.add(tuple(sorted((str(k), str(v)) for k, v in ace.items())))
    return out


async def _drain(repo: FilesRepo) -> None:
    """Run every outstanding subtree repair to completion, as a worker would.

    The worker owns a session of its own, so it starts holding no nodes at all.
    Detaching first reproduces that: a session that still held the rows a move
    just re-pathed would hand the repair its own stale copies, and the repair
    would recompute each node's chain from where it used to be.
    """
    repo.session.expunge_all()
    async with repo.transaction():
        pending = list(
            (await repo.execute_scoped(select(FileOp.id).where(FileOp.kind == acl.OP_ACL_REWRITE)))
            .scalars()
            .all()
        )
    for op_id in pending:
        for _ in range(50):
            async with repo.transaction():
                moved = await acl.rewrite(repo, OperationId(op_id), batch=64)
            if moved == 0:
                break


async def _node(repo: FilesRepo, node_id: uuid.UUID) -> FileNode:
    """Re-read one node from the database, never from the identity map.

    The ACL rewrite and the move are ``UPDATE`` statements rather than ORM
    flushes, so an instance the session already holds keeps the values it was
    loaded with — an assertion made against it would be an assertion about the
    past.
    """
    got = await repo.session.get(FileNode, node_id, populate_existing=True)
    assert got is not None
    return got


async def _live_nodes(repo: FilesRepo, drive: FileDrive) -> list[FileNode]:
    rows = (
        await repo.execute_scoped(
            select(FileNode)
            .execution_options(populate_existing=True)
            .where(
                FileNode.drive_id == drive.id,
                FileNode.trashed_at.is_(None),
                FileNode.traversal_only.is_(False),
            )
        )
    ).scalars()
    return list(rows)


async def _assert_invariants(
    repo: FilesRepo, org: FilesOrg, drive: FileDrive, source: GrantSource
) -> None:
    """The three checks, run after every operation of the generated history."""
    async with repo.transaction():
        nodes = await _live_nodes(repo, drive)
        for node in nodes:
            chain = await repo.chain(node)
            # 1. trickle-down: no ancestor outranks its own descendant.
            for user_id in (org.admin_id, org.member_id):
                mine = await _role_of(repo, source, org, drive, node, user_id)
                for ancestor in chain:
                    if ancestor.id == node.id:
                        continue
                    theirs = await _role_of(repo, source, org, drive, ancestor, user_id)
                    assert DEFAULT_LADDER.rank(mine) >= DEFAULT_LADDER.rank(theirs), (
                        f"{node.id} ranks {mine!r} under ancestor {ancestor.id} at {theirs!r}"
                    )
            # 2. the cache is exactly the chain union.
            if node.acl_id is not None and node.state != ACL_REWRITING:
                cached = await repo.acl(node.acl_id)
                assert cached is not None
                truth = await acl.effective_body(repo, node, chain)
                assert await _ace_keys(cached.body) == await _ace_keys(truth.as_json()), (
                    f"stale cache on {node.id}"
                )


async def _assert_inherited_revoke_is_refused(
    repo: FilesRepo, org: FilesOrg, drive: FileDrive
) -> None:
    """3. Every share held by a strict ancestor is unrevokable from below, and
    the refusal names where to go instead."""
    async with repo.transaction():
        nodes = await _live_nodes(repo, drive)
        for node in nodes:
            chain = await repo.chain(node)
            for ancestor in chain:
                if ancestor.id == node.id:
                    continue
                for share in await repo.shares_of(NodeId(ancestor.id)):
                    with pytest.raises(acl.InheritedGrant) as caught:
                        await acl.revoke(repo, _ctx(org), node, share.id)
                    assert caught.value.detail["granting_node_id"] == str(ancestor.id)
                    assert caught.value.code == "files.inherited_grant"
                    return


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


async def _materialize(
    namespace: Namespace, repo: FilesRepo, drive: FileDrive, paths: Sequence[tuple[bytes, ...]]
) -> dict[tuple[bytes, ...], FileNode]:
    """Create the generated tree through the real namespace, skipping the nodes
    the tree strategy is allowed to make that the namespace refuses."""
    assert drive.root_node_id is not None
    made: dict[tuple[bytes, ...], FileNode] = {}
    async with repo.transaction():
        for path in paths:
            parent = made.get(path[:-1])
            parent_id = NodeId(parent.id if parent is not None else drive.root_node_id)
            if path[:-1] and parent is None:
                continue
            try:
                names.validate(path[-1])
            except errors.FilesError:
                continue
            try:
                made[path] = await namespace.create(
                    DriveId(drive.id), parent_id, "folder", path[-1], conflict="fail"
                )
            except errors.FilesError:
                continue
    return made


async def _apply(
    repo: FilesRepo,
    namespace: Namespace,
    org: FilesOrg,
    drive: FileDrive,
    op: tuple[Any, ...],
    grant_specs: Sequence[GrantSpec],
    seq: int,
) -> None:
    """Apply one generated operation, re-targeted onto a live node.

    A generated op names a path in the strategy's own tree; the tree that
    actually exists is whatever the namespace accepted, so the op's *verb* and
    its ordering are what the property consumes, with the target chosen
    deterministically from the live nodes.
    """
    tag = str(op[0])
    async with repo.transaction():
        live = await _live_nodes(repo, drive)
    if not live:
        return
    target = live[seq % len(live)]
    if tag == "create":
        raw = op[2] if isinstance(op[2], bytes) else b"child"
        try:
            names.validate(raw)
        except errors.FilesError:
            return
        async with repo.transaction():
            try:
                await namespace.create(
                    DriveId(drive.id), NodeId(target.id), "folder", raw, conflict="rename"
                )
            except errors.FilesError:
                return
    elif tag == "rename":
        raw = op[2] if isinstance(op[2], bytes) else b"renamed"
        try:
            names.validate(raw)
        except errors.FilesError:
            return
        async with repo.transaction():
            try:
                await namespace.rename(
                    NodeId(target.id), raw, if_match=target.etag, conflict="rename"
                )
            except errors.FilesError:
                return
    elif tag == "move":
        parent = live[(seq + 1) % len(live)]
        if parent.id == target.id:
            return
        async with repo.transaction():
            try:
                # The move re-derives the subtree's inherited ACEs itself; the
                # sequence player used to have to do it by hand.
                await namespace.move(NodeId(target.id), NodeId(parent.id), if_match=target.etag)
            except errors.FilesError:
                return
    elif tag == "grant":
        spec = grant_specs[seq % len(grant_specs)]
        principal = _principals(org)[seq % 3]
        async with repo.transaction():
            try:
                await acl.grant(repo, _ctx(org), target, principal, spec.role)
            except errors.FilesError:
                return
    elif tag == "revoke":
        async with repo.transaction():
            shares = await repo.shares_of(NodeId(target.id))
            if not shares:
                return
            try:
                await acl.revoke(repo, _ctx(org), target, shares[0].id)
            except errors.FilesError:
                return


async def _play(
    engine: AsyncEngine,
    tree_paths: Sequence[tuple[bytes, ...]],
    ops: Sequence[tuple[Any, ...]],
    grant_specs: Sequence[GrantSpec],
) -> None:
    """One example: a fresh tenant, a fresh drive, the generated history."""
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        org = await _seed_org(session)
        repo = FilesRepo(session, OrgScope(org_team_id=org.org_team_id))
        factory = FilesFactory(session, org)
        drive = await factory.drive(org=org)
        namespace = Namespace(repo, _ctx(org), FakeClock(now=EPOCH), None)
        source: GrantSource = FilesGrantSource()

        await _materialize(namespace, repo, drive, tree_paths)
        await _drain(repo)
        await _assert_invariants(repo, org, drive, source)

        for seq, op in enumerate(ops):
            await _apply(repo, namespace, org, drive, op, grant_specs, seq)
            await _drain(repo)
            await _assert_invariants(repo, org, drive, source)
        await _assert_inherited_revoke_is_refused(repo, org, drive)
    finally:
        await session.close()


@pytest.fixture
def play() -> Iterator[Callable[..., None]]:
    """One event loop and one pooled connection for every example below.

    Hypothesis calls the test once per example, and ``asyncio.run`` around a
    freshly built engine made each of the forty examples pay for a TCP connect,
    a forked Postgres backend and an authentication round-trip — forty of each
    for one test, and on CI every worker doing that at once. asyncpg binds a
    connection to the loop that opened it, so sharing the engine means owning
    the loop too: both are built here and every example runs on them.

    Nothing about an example's isolation changes — each one still seeds its own
    tenant and its own drive and never looks at another's rows — which is why
    the health check that would refuse a function-scoped fixture under ``given``
    is suppressed below rather than obeyed.
    """
    loop = asyncio.new_event_loop()
    engine = create_async_engine(
        settings.database_url, pool_size=1, max_overflow=0, pool_pre_ping=True
    )
    try:
        yield lambda *example: loop.run_until_complete(_play(engine, *example))
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()


@hypothesis_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(
    tree=trees(max_nodes=4),
    history=op_sequences(max_ops=6, max_nodes=4),
    grant_specs=st.lists(grants(), min_size=1, max_size=3),
)
def test_trickle_down_survives_every_generated_history(
    play: Callable[..., None], tree: Any, history: Any, grant_specs: list[GrantSpec]
) -> None:
    """No ancestor ever outranks its descendant, and no cache ever outranks the
    chain, across generated trees, grants and operation sequences."""
    base, ops = history
    paths = (*tree.paths(), *base.paths())
    play(paths, ops, grant_specs)


# ---------------------------------------------------------------------------
# The two consequences, pinned deterministically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoking_at_the_ancestor_removes_the_grant_below_it(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The refusal from below has a remedy: revoke where it was granted."""
    drive = await files_factory.drive()
    made = await files_factory.tree("top/ top/mid/ top/mid/leaf.txt", drive=drive)
    top, leaf = made["top"], made["top/mid/leaf.txt"]
    source: GrantSource = FilesGrantSource()

    async with repo.transaction():
        share = await acl.grant(
            repo, _ctx(files_org), top, Principal(kind="user", id=files_org.member_id), "writer"
        )
    await _drain(repo)
    async with repo.transaction():
        fresh = await _node(repo, leaf.id)
        assert (
            await _role_of(repo, source, files_org, drive, fresh, files_org.member_id) == "writer"
        )

    async with repo.transaction():
        top_now = await _node(repo, top.id)
        await acl.revoke(repo, _ctx(files_org), top_now, share.id)
    await _drain(repo)

    async with repo.transaction():
        fresh = await _node(repo, leaf.id)
        assert await _role_of(repo, source, files_org, drive, fresh, files_org.member_id) is None
        cached_truth = await acl.effective_body(repo, fresh, await repo.chain(fresh))
        assert await _ace_keys(cached_truth.as_json()) == set()


@pytest.mark.asyncio
async def test_moving_a_subtree_out_drops_the_old_ancestors_grant(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """A move is a change of chain, so it is a change of permissions — for the
    moved node *and* everything under it."""
    drive = await files_factory.drive()
    made = await files_factory.tree(
        "shared/ shared/sub/ shared/sub/deep.txt elsewhere/", drive=drive
    )
    shared, sub, deep, elsewhere = (
        made["shared"],
        made["shared/sub"],
        made["shared/sub/deep.txt"],
        made["elsewhere"],
    )
    source: GrantSource = FilesGrantSource()
    namespace = Namespace(repo, _ctx(files_org), FakeClock(now=EPOCH), None)

    async with repo.transaction():
        await acl.grant(
            repo, _ctx(files_org), shared, Principal(kind="user", id=files_org.member_id), "reader"
        )
    await _drain(repo)
    async with repo.transaction():
        under = await _node(repo, deep.id)
        assert (
            await _role_of(repo, source, files_org, drive, under, files_org.member_id) == "reader"
        )

    async with repo.transaction():
        current = await _node(repo, sub.id)
        await namespace.move(NodeId(sub.id), NodeId(elsewhere.id), if_match=current.etag)
    await _drain(repo)

    async with repo.transaction():
        for label, node_id in (("sub", sub.id), ("deep", deep.id)):
            after = await _node(repo, node_id)
            role = await _role_of(repo, source, files_org, drive, after, files_org.member_id)
            assert role is None, f"{label} still {role!r} after the move"


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        pytest.param("reader", "reader", id="reader-trickles"),
        pytest.param("owner", "owner", id="owner-trickles"),
    ],
)
@pytest.mark.asyncio
async def test_a_grant_on_an_ancestor_is_the_role_below_it(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    role: str,
    expected: str,
) -> None:
    """The floor case the property generalizes: whatever the ancestor grants is
    at least what the descendant has."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b/ a/b/c.txt", drive=drive)
    source: GrantSource = FilesGrantSource()
    async with repo.transaction():
        await acl.grant(
            repo, _ctx(files_org), made["a"], Principal(kind="user", id=files_org.member_id), role
        )
    await _drain(repo)
    async with repo.transaction():
        leaf = await _node(repo, made["a/b/c.txt"].id)
        assert await _role_of(repo, source, files_org, drive, leaf, files_org.member_id) == expected


@pytest.mark.asyncio
async def test_a_revoke_from_below_names_the_ancestor_and_changes_nothing(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """Files has no deny entries, so shadowing an inherited grant is refused —
    and the refused call leaves the grant exactly as it was."""
    drive = await files_factory.drive()
    made = await files_factory.tree("root/ root/child.txt", drive=drive)
    source: GrantSource = FilesGrantSource()
    async with repo.transaction():
        share = await acl.grant(
            repo,
            _ctx(files_org),
            made["root"],
            Principal(kind="user", id=files_org.member_id),
            "writer",
        )
    await _drain(repo)

    async with repo.transaction():
        child = await _node(repo, made["root/child.txt"].id)
        with pytest.raises(acl.InheritedGrant) as caught:
            await acl.revoke(repo, _ctx(files_org), child, share.id)
    assert caught.value.detail["granting_node_id"] == str(made["root"].id)

    async with repo.transaction():
        still = (
            await repo.execute_scoped(select(FileShare).where(FileShare.id == share.id))
        ).scalar_one()
        assert still.revoked_at is None
        child = await _node(repo, made["root/child.txt"].id)
        assert (
            await _role_of(repo, source, files_org, drive, child, files_org.member_id) == "writer"
        )


@pytest.mark.parametrize(
    "held_flags",
    [
        pytest.param(0, id="an-ordinary-held-folder"),
        pytest.param(NO_DOWNLOAD_BIT, id="a-held-folder-nobody-may-download-from"),
    ],
)
@pytest.mark.parametrize(
    "role",
    [
        pytest.param("reader", id="a-reader-grant-above-the-held-folder"),
        pytest.param("writer", id="a-writer-grant-above-the-held-folder"),
    ],
)
@pytest.mark.asyncio
async def test_a_row_the_holder_mints_carries_its_folders_grants_and_nothing_else(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, role: str, held_flags: int
) -> None:
    """A file the holder reports before its bytes is minted by the tree report,
    not by the create every other test here drives, and it must be born the
    same way: no grant of its own, no cached ACL of its own, the restricting
    flags its folder passes down -- and so exactly the access its folder has,
    read through the chain. Revoking at the ancestor proves it is the chain and
    not a copy taken at birth."""
    drive = await files_factory.drive()
    made = await files_factory.tree("shared/ shared/box/", drive=drive)
    box_id, shared = made["shared/box"].id, made["shared"]
    source: GrantSource = FilesGrantSource()
    clock = FakeClock(now=EPOCH)
    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_nodes SET flags = :flags WHERE id = :n"),
            {"flags": held_flags, "n": box_id},
        )
    async with repo.transaction():
        share = await acl.grant(
            repo, _ctx(files_org), shared, Principal(kind="user", id=files_org.member_id), role
        )
    await _drain(repo)
    async with repo.transaction():
        grant = await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(box_id), instance_id="box-1", machine_id="box-7"
        )
        await repo.session.execute(
            text(
                "UPDATE file_leases SET live_cadence = '{\"on\": true}'::jsonb WHERE node_id = :n"
            ),
            {"n": box_id},
        )
    async with repo.transaction():
        await LeaseTreeService(repo, _ctx(files_org), clock).apply(
            NodeId(box_id),
            drive_id=DriveId(drive.id),
            epoch=grant.epoch,
            instance_id="box-1",
            batch_id=uuid.uuid4(),
            changes=[
                TreeChange(op="upsert", path=b"out/report.csv", kind="file", size=3, mtime_ns=1)
            ],
        )

    async with repo.transaction():
        minted = (
            await repo.execute_scoped(
                select(FileNode)
                .execution_options(populate_existing=True)
                .where(FileNode.drive_id == drive.id, FileNode.name == b"report.csv")
            )
        ).scalar_one()
        folder = await _node(repo, minted.parent_id)
        assert minted.head_version_id is None
        assert await repo.shares_of(NodeId(minted.id)) == []
        assert minted.acl_id is None and minted.default_acl_id is None
        assert minted.flags == flags_for_child(folder.flags)
        # Both the folder the report made on the way and the file in it keep
        # what the held folder passes down: a folder nobody may download from
        # does not grow a downloadable file because a machine listed it.
        assert minted.flags & NO_DOWNLOAD_BIT == held_flags & NO_DOWNLOAD_BIT
        assert folder.flags & NO_DOWNLOAD_BIT == held_flags & NO_DOWNLOAD_BIT
        for user_id in (files_org.admin_id, files_org.member_id):
            assert await _role_of(repo, source, files_org, drive, minted, user_id) == (
                await _role_of(repo, source, files_org, drive, folder, user_id)
            )
        assert await _role_of(repo, source, files_org, drive, minted, files_org.member_id) == role

    async with repo.transaction():
        await acl.revoke(repo, _ctx(files_org), await _node(repo, shared.id), share.id)
    await _drain(repo)
    async with repo.transaction():
        minted_now = await _node(repo, minted.id)
        assert (
            await _role_of(repo, source, files_org, drive, minted_now, files_org.member_id) is None
        )

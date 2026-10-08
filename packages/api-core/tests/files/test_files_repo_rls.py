"""The repo's org predicate and the RLS policy behind it, against real Postgres.

Two independent nets are under test here and the tests keep them apart on
purpose: the repo's own ``org_team_id`` predicate (a bug in application code
defeats it) and the database policy (a bug in application code does not). The
raw-SQL cases deliberately bypass the repo so the policy is proven on its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.repo import (
    APP_ROLE,
    FilesRepo,
    RepoUsageError,
    ScopeError,
)
from alkera_core.models.files.tree import FileNode
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


async def test_repo_never_returns_another_orgs_node(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """A node seeded for org B is invisible to org A's repo, by id and in bulk."""
    other = await files_org_factory()
    drive_a = await files_factory.drive()
    drive_b = await files_factory.drive(org=other)
    tree_a = await files_factory.tree("a.txt", drive=drive_a)
    tree_b = await files_factory.tree("b.txt", drive=drive_b)

    async with repo.transaction():
        assert await repo.node(NodeId(tree_a["a.txt"].id)) is not None
        assert await repo.node(NodeId(tree_b["b.txt"].id)) is None
        both = await repo.nodes([NodeId(tree_a["a.txt"].id), NodeId(tree_b["b.txt"].id)])
        assert [row.id for row in both] == [tree_a["a.txt"].id]
        assert await repo.drive(DriveId(drive_b.id)) is None
        assert (await repo.drive_for_org()) is not None
        assert (await repo.drive_for_org()).id == drive_a.id  # type: ignore[union-attr]


async def test_raw_select_under_the_app_role_with_no_org_setting_returns_nothing(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
) -> None:
    """The policy, not the repo: raw SQL with the setting unset sees zero rows.

    The same statement with the setting stamped sees exactly the org's rows, so
    the emptiness is the policy binding rather than an empty table.
    """
    drive = await files_factory.drive()
    await files_factory.tree("a.txt b.txt", drive=drive)

    await files_session.begin()
    await files_session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    unset = (await files_session.execute(text("SELECT count(*) FROM file_nodes"))).scalar_one()
    await files_session.execute(
        text("SELECT set_config('alkera.org_id', :org, true)"),
        {"org": str(files_org.org_team_id)},
    )
    stamped = (await files_session.execute(text("SELECT count(*) FROM file_nodes"))).scalar_one()
    await files_session.rollback()

    assert unset == 0
    assert stamped == 3  # the drive root plus the two files


async def test_rls_binds_even_though_the_login_role_bypasses_it(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
) -> None:
    """Why every Files transaction assumes ``alkera_files_app``, end to end.

    The login bypasses RLS outright — the local one is a superuser, a
    deployment's is ``BYPASSRLS`` (the tier ``make test-rls-login`` runs this
    as that shape) — so the policy would be a no-op if the transaction did not
    first assume the tenant role. This pins both halves: the login DOES
    bypass, and after ``SET LOCAL ROLE`` the other org's rows are gone anyway.
    """
    bypasses = (
        await files_session.execute(
            text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
    ).scalar_one()
    assert bypasses is True, "this test only means something on a login that bypasses RLS"

    other = await files_org_factory()
    drive_a = await files_factory.drive()
    drive_b = await files_factory.drive(org=other)
    await files_factory.tree("a.txt", drive=drive_a)
    await files_factory.tree("b.txt", drive=drive_b)

    await files_session.begin()
    await files_session.execute(
        text("SELECT set_config('alkera.org_id', :org, true)"),
        {"org": str(files_org.org_team_id)},
    )
    as_superuser = (
        await files_session.execute(
            text("SELECT count(*) FROM file_nodes WHERE org_team_id = :org"),
            {"org": str(other.org_team_id)},
        )
    ).scalar_one()
    await files_session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    as_app_role = (
        await files_session.execute(
            text("SELECT count(*) FROM file_nodes WHERE org_team_id = :org"),
            {"org": str(other.org_team_id)},
        )
    ).scalar_one()
    await files_session.rollback()

    assert as_superuser == 2, "the superuser login bypasses the policy"
    assert as_app_role == 0, "assuming the app role is what makes the policy bind"


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda r: r.node(NodeId(uuid.uuid4())), id="node"),
        pytest.param(lambda r: r.nodes([NodeId(uuid.uuid4())]), id="nodes"),
        pytest.param(lambda r: r.drive(DriveId(uuid.uuid4())), id="drive"),
        pytest.param(lambda r: r.drive_for_org(), id="drive_for_org"),
        pytest.param(lambda r: r.lock_node(NodeId(uuid.uuid4())), id="lock_node"),
        pytest.param(lambda r: r.lock_drive(DriveId(uuid.uuid4())), id="lock_drive"),
        pytest.param(
            lambda r: r.children_page(NodeId(uuid.uuid4()), after=None, limit=1),
            id="children_page",
        ),
        pytest.param(lambda r: r.versions_of(NodeId(uuid.uuid4())), id="versions_of"),
        pytest.param(lambda r: r.shares_of(NodeId(uuid.uuid4())), id="shares_of"),
        pytest.param(lambda r: r.flush(), id="flush"),
        pytest.param(lambda r: r.execute_scoped(select(FileNode)), id="execute_scoped"),
    ],
)
async def test_every_method_refuses_outside_the_transaction(
    repo: FilesRepo,
    call: Callable[[FilesRepo], Awaitable[object]],
) -> None:
    """Outside ``transaction()`` neither the role nor the org setting is in
    force, so running the statement would be running it unprotected."""
    with pytest.raises(RepoUsageError):
        await call(repo)


async def test_the_same_method_works_inside_the_transaction(repo: FilesRepo) -> None:
    """The negative twin of the refusal: the guard is the transaction, not the
    method — the identical call inside one runs and answers."""
    async with repo.transaction():
        assert await repo.node(NodeId(uuid.uuid4())) is None


async def test_chain_returns_the_ancestors_root_first(
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """A depth-5 node's chain is root → … → itself, in path order.

    The expected order is computed from the parent pointers, independently of
    ``path_ids`` — the column the repo reads — so a chain built from a wrong
    ``path_ids`` would disagree.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("a/ a/b/ a/b/c/ a/b/c/d/ a/b/c/d/e.txt", drive=drive)
    leaf = tree["a/b/c/d/e.txt"]

    async with repo.transaction():
        chain = await repo.chain(leaf)
        by_id = {node.id: node for node in chain}
        walk: list[uuid.UUID] = []
        cursor: FileNode | None = leaf
        while cursor is not None:
            walk.append(cursor.id)
            parent_id = cursor.parent_id
            cursor = by_id.get(parent_id) if parent_id is not None else None

    assert [node.id for node in chain] == list(reversed(walk))
    assert [node.depth for node in chain] == [0, 1, 2, 3, 4, 5]
    assert chain[-1].id == leaf.id


async def test_children_page_keyset_visits_every_child_exactly_once(
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """30 children paged at 7 come back once each, in key order.

    The names are deliberately not in insertion order and share prefixes, so a
    page boundary that fell inside an equal-``name_key`` run would show up as a
    duplicate or a hole.
    """
    drive = await files_factory.drive()
    names = [f"f{index:02d}.txt" for index in range(30)]
    tree = await files_factory.tree(" ".join(sorted(names, reverse=True)), drive=drive)
    parent = NodeId(drive.root_node_id)  # type: ignore[arg-type]

    seen: list[uuid.UUID] = []
    async with repo.transaction():
        after: tuple[str, uuid.UUID] | None = None
        while True:
            page = await repo.children_page(parent, after=after, limit=7)
            if not page:
                break
            assert len(page) <= 7
            seen.extend(node.id for node in page)
            after = (page[-1].name_key, page[-1].id)

    assert len(seen) == len(set(seen)) == 30
    assert seen == [tree[name].id for name in sorted(names)]


async def test_children_page_skips_trashed_children(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """The page is live-only: a trashed sibling never appears, and the rest of
    the page is unaffected by its absence."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("keep.txt gone.txt", drive=drive)
    await files_session.execute(
        text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"),
        {"id": tree["gone.txt"].id},
    )
    await files_session.commit()

    async with repo.transaction():
        page = await repo.children_page(
            NodeId(drive.root_node_id),  # type: ignore[arg-type]
            after=None,
            limit=10,
        )

    assert [node.id for node in page] == [tree["keep.txt"].id]


async def test_execute_scoped_appends_the_org_predicate(
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """A hand-written SELECT with no org filter still reads only this org."""
    other = await files_org_factory()
    drive_a = await files_factory.drive()
    drive_b = await files_factory.drive(org=other)
    tree_a = await files_factory.tree("a.txt", drive=drive_a)
    await files_factory.tree("b.txt", drive=drive_b)

    async with repo.transaction():
        result = await repo.execute_scoped(select(FileNode).where(FileNode.kind == "file"))
        rows = list(result.scalars().all())

    assert [row.id for row in rows] == [tree_a["a.txt"].id]


async def test_execute_scoped_refuses_a_statement_it_cannot_scope(repo: FilesRepo) -> None:
    """A non-Files table has no ``org_team_id`` to constrain, so the statement
    is refused rather than run unscoped."""
    from alkera_core.models.team import Team

    async with repo.transaction():
        with pytest.raises(ScopeError):
            await repo.execute_scoped(select(Team))


def _short_label(ino: int) -> str:
    """A node's ``path_ids`` label: its per-drive ino in base36.

    Short by construction, which is the whole point — a GiST key holds the value
    it indexes, and a path of 33-byte UUID labels cannot be placed on a page once
    a tree is a few dozen levels deep.
    """
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if ino == 0:
        return "0"
    out: list[str] = []
    while ino:
        ino, remainder = divmod(ino, 36)
        out.append(digits[remainder])
    return "".join(reversed(out))


async def _seed_chain(
    session: AsyncSession,
    drive_id: uuid.UUID,
    org_id: uuid.UUID,
    root: FileNode,
    depth: int,
    *,
    first_ino: int,
) -> list[uuid.UUID]:
    """Append ``depth`` nodes below ``root``, one per level, and return their ids."""
    parent_path = root.path_ids
    parent_id = root.id
    parent_depth = root.depth
    made: list[uuid.UUID] = []
    for step in range(depth):
        node_id = uuid.uuid4()
        ino = first_ino + step
        parent_path = f"{parent_path}.{_short_label(ino)}"
        parent_depth += 1
        await session.execute(
            text(
                "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, "
                "name, name_display, name_key, flags_names, path_ids, depth, mode, uid, gid, "
                "nlink, size, rdev, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, "
                "flags, traversal_only, metadata) VALUES "
                "(:id, :ino, :drive, :org, :parent, 'folder', :name, :display, :display, "
                "'{}'::jsonb, CAST(:path AS ltree), :depth, 493, 0, 0, 1, 0, 0, 0, 0, 0, 0, "
                "'{}'::jsonb, 1, 0, false, '{}'::jsonb)"
            ),
            {
                "id": node_id,
                "ino": ino,
                "drive": drive_id,
                "org": org_id,
                "parent": parent_id,
                "name": f"d{ino}".encode(),
                "display": f"d{ino}",
                "path": parent_path,
                "depth": parent_depth,
            },
        )
        made.append(node_id)
        parent_id = node_id
    await session.commit()
    return made


async def _drop_chain(session: AsyncSession, drive_id: uuid.UUID) -> None:
    """Delete a seeded drive's nodes, deepest first.

    A deep chain is cleaned up rather than left behind because the suite shares
    one database: a 1,024-deep tree left in ``file_nodes`` is exactly what an
    index over the untruncated path cannot be built over, so the migration
    round-trip test would fail on this test's leftovers instead of on its own
    subject.
    """
    await session.execute(
        text("DELETE FROM file_nodes WHERE drive_id = :drive AND parent_id IS NOT NULL"),
        {"drive": drive_id},
    )
    await session.commit()


async def test_a_chain_1024_deep_is_accepted_and_read_back_by_subtree(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """The spec accepts a path 1,024 segments deep, so the index must too.

    An index over the untruncated ``path_ids`` refuses the insert long before
    this depth — "failed to add item to index page" — and the table then refuses
    every further insert until it is rebuilt. Reaching 1,024 at all is therefore
    the assertion; reading the whole chain back through the repo proves the
    truncated key lost none of it.
    """
    drive = await files_factory.drive()
    async with repo.transaction():
        root = await repo.node(NodeId(drive.root_node_id))
    assert root is not None
    made = await _seed_chain(
        files_session, drive.id, files_org.org_team_id, root, 1_024, first_ino=1_000
    )

    async with repo.transaction():
        under_root = await repo.subtree_page(root, limit=2_000)
        deep = await repo.node(NodeId(made[-1]))
    assert deep is not None
    await _drop_chain(files_session, drive.id)

    assert deep.depth == 1_024
    assert {row.id for row in under_root} == {root.id, *made}


async def test_a_subtree_query_under_a_deep_root_returns_exactly_its_descendants(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """A root at depth 200 is past the index's 128-label key, so the truncated
    predicate alone also admits its cousins; the exact filter must remove them.

    The two chains diverge at depth 150 — inside the region the truncation
    cannot see — so a predicate that dropped the exact filter would return both
    and this test would fail while every shallow case still passed.
    """
    drive = await files_factory.drive()
    async with repo.transaction():
        root = await repo.node(NodeId(drive.root_node_id))
    assert root is not None
    trunk = await _seed_chain(
        files_session, drive.id, files_org.org_team_id, root, 149, first_ino=1_000
    )

    async with repo.transaction():
        fork = await repo.node(NodeId(trunk[-1]))
    assert fork is not None
    mine = await _seed_chain(
        files_session, drive.id, files_org.org_team_id, fork, 60, first_ino=5_000
    )
    cousins = await _seed_chain(
        files_session, drive.id, files_org.org_team_id, fork, 60, first_ino=9_000
    )

    async with repo.transaction():
        deep_root = await repo.node(NodeId(mine[50]))
        assert deep_root is not None
        assert deep_root.depth == 200
        found = await repo.subtree_page(deep_root, limit=2_000)

    await _drop_chain(files_session, drive.id)

    assert {row.id for row in found} == {deep_root.id, *mine[51:]}
    assert not {row.id for row in found} & set(cousins)


async def test_a_shallow_subtree_query_is_served_by_the_index(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """The predicate is written against the indexed expression, so the planner
    can reach it. A predicate that had drifted from the index would still be
    correct and would still pass every case above — this is the one that fails.
    """
    drive = await files_factory.drive()
    async with repo.transaction():
        root = await repo.node(NodeId(drive.root_node_id))
    assert root is not None
    await _seed_chain(files_session, drive.id, files_org.org_team_id, root, 40, first_ino=1_000)

    # The repo's own predicate is compiled and EXPLAINed verbatim — a hand-typed
    # copy of it would keep passing after the repo's expression had drifted away
    # from the index.
    compiled = (
        select(FileNode.id)
        .where(repo.subtree_predicate(root))
        .compile(dialect=postgresql.dialect(paramstyle="named"))
    )
    await files_session.begin()
    # A tiny table is a sequential scan whatever the index says, so the plan is
    # asked for with the alternative disabled: what is proven is that the index
    # CAN serve this predicate, not what the planner costs it at today. The org
    # predicate is left off deliberately — with it the planner reaches for
    # ``ix_file_nodes_org_team_id`` and this test would say nothing about the
    # subtree index at all.
    await files_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        str(line)
        for line in (
            await files_session.execute(text(f"EXPLAIN {compiled}"), dict(compiled.params))
        )
        .scalars()
        .all()
    )
    await files_session.rollback()
    assert "Index" in plan, plan
    assert "ix_file_nodes_path_ids" in plan, plan


async def test_the_policy_binds_the_page_grants_table_in_both_directions(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
) -> None:
    """A page grant is a read capability, so its table gets the same two nets.

    Raw SQL under the app role, deliberately bypassing the repo: another org's
    grant is invisible even when named by its own nonce (the USING half), and a
    grant cannot be written into another org's name either (the WITH CHECK
    half) — which is what stops a page being opened across a tenant boundary.
    """
    other = await files_org_factory()
    drive = await files_factory.drive()
    tree = await files_factory.tree("page.html", drive=drive)
    node_id = tree["page.html"].id
    insert = (
        "INSERT INTO file_page_grants (id, org_team_id, nonce, root_node_id, "
        "entry_node_id, minted_by_user_id, expires_at) VALUES "
        "(:id, :org, :nonce, :node, :node, :user, now() + interval '15 minutes')"
    )
    planted = {
        "id": uuid.uuid4(),
        "org": other.org_team_id,
        "nonce": uuid.uuid4().hex,
        "node": node_id,
        "user": other.admin_id,
    }
    await files_session.execute(text(insert), planted)
    await files_session.commit()

    await files_session.begin()
    await files_session.execute(
        text("SELECT set_config('alkera.org_id', :org, true)"),
        {"org": str(files_org.org_team_id)},
    )
    await files_session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    visible = (
        await files_session.execute(
            text("SELECT count(*) FROM file_page_grants WHERE nonce = :nonce"),
            {"nonce": planted["nonce"]},
        )
    ).scalar_one()
    with pytest.raises(ProgrammingError):
        await files_session.execute(
            text(insert), {**planted, "id": uuid.uuid4(), "nonce": uuid.uuid4().hex}
        )
    await files_session.rollback()

    assert visible == 0, "another org's grant is readable under the policy"

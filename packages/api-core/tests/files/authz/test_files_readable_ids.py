"""``readable_ids`` decides a page exactly as the per-node path does, in four
statements.

The seam exists to delete an N+1, so it is worth nothing unless it is *equal*
to the loop it replaces. Both claims are pinned here against real rows on the
lane database: a randomized property over pages built from grants, teams,
inheritance, expiry and node flags asserts the two answers agree element for
element, and a 10,000-id page is counted at the driver to assert the cost does
not grow with the page.

The reference side is deliberately the existing production path — ``repo.chain``
plus ``FilesGrantSource`` plus ``effective_role`` — not a re-implementation of
the batched loader, so the test cannot agree with the code by construction.
"""

from __future__ import annotations

import contextlib
import math
import random
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from alkera_core.authz.principal import ActingContext
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.decider import (
    HELD_BIT,
    NO_DOWNLOAD_BIT,
    AccessFacts,
    EffectiveAccess,
    effective_role,
)
from alkera_core.files.authz.grants import ACL_REWRITING, FilesGrantSource
from alkera_core.files.authz.readable import decided_by_id, readable_ids
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import APP_ROLE, ID_BATCH, FilesRepo
from alkera_core.models.files.acl import FileAcl
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from sqlalchemy import delete, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: Transaction control carries no work, so it is not counted against the budget.
_CONTROL = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"})

#: The statement budget for one page, whatever the page holds: the page's own
#: rows, their chains, the interned bodies, and the shares of the chains whose
#: cache is stale.
#:
#: 3 -> 4. The chain used to ride on the page's own statement as an ltree join.
#: That join is index-served for the table owner and not for ``alkera_files_app``,
#: where FORCE row security stops PostgreSQL promoting a non-leakproof qual —
#: which ``subpath()`` and ltree containment are — ahead of the policy's own.
#: Addressed by ``(drive_id, ino)`` instead it keeps its unique index under the
#: role, at the price of reading the anchors first. What still does the work here
#: is that the number does not move with the size of the page.
MAX_STATEMENTS = 4


def _ctx(org: FilesOrg, user_id: uuid.UUID) -> ActingContext:
    return ActingContext.for_user(user_id=user_id, org_id=org.org_team_id, email="m@files.test")


@contextmanager
def counting(engine: Engine) -> Iterator[list[str]]:
    seen: list[str] = []

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if statement.strip().split(" ", 1)[0].upper() in _CONTROL:
            return
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


#: One statement that builds a run of sibling leaves from seven arrays.
#:
#: The tail of literals is every ``file_nodes`` column the MODEL fills rather
#: than the server — POSIX mode 0644, one link, and the three empty JSONB bags —
#: spelled out because these rows are assembled in SQL, where the ORM's defaults
#: do not apply. A new NOT NULL column with a Python-side default makes the two
#: tests below fail on the not-null, which is when this list gains an entry.
_BULK_LEAVES = text(
    "INSERT INTO file_nodes ("
    " id, ino, drive_id, org_team_id, parent_id, name, name_display, name_key,"
    " path_ids, depth, acl_id,"
    " kind, mode, uid, gid, nlink, size, rdev, atime_ns, mtime_ns, ctime_ns,"
    " birthtime_ns, etag, flags, traversal_only, flags_names, xattrs, metadata)"
    " SELECT leaf.id, leaf.ino, :drive_id, :org_team_id, :parent_id,"
    " leaf.name, leaf.name_display, leaf.name_key, leaf.path_ids::ltree,"
    " :depth, leaf.acl_id,"
    " 'file', 420, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, false,"
    " '{}'::jsonb, '{}'::jsonb, '{}'::jsonb"
    " FROM unnest(CAST(:ids AS uuid[]), CAST(:inos AS bigint[]),"
    " CAST(:names AS bytea[]), CAST(:displays AS text[]), CAST(:keys AS text[]),"
    " CAST(:paths AS text[]), CAST(:acls AS uuid[]))"
    " AS leaf(id, ino, name, name_display, name_key, path_ids, acl_id)"
)


async def _bulk_leaves(
    session: AsyncSession,
    drive: FileDrive,
    folder: FileNode,
    count: int,
    *,
    stem: str,
    acl_of: Callable[[int], uuid.UUID | None] = lambda _offset: None,
) -> list[uuid.UUID]:
    """Put ``count`` sibling files under ``folder`` and return their ids.

    One statement over seven arrays, not ``count`` mapped instances: what these
    two tests need is that the rows exist — neither ever touches an attribute
    afterwards — and ten thousand ``FileNode`` objects cost ten thousand
    identity-map entries, a flush that walks every one of them and a parameter
    set per row on the wire. Measured on ten thousand leaves: 2.0 s through the
    ORM, 0.6 s through this.

    The inos are a contiguous block from the drive's counter, which is what lets
    the caller take them back out again through ``uq_file_nodes_drive_ino``
    instead of naming three thousand ids.
    """
    start = int(drive.next_ino)
    ids = [uuid.uuid4() for _ in range(count)]
    names = [f"{stem}{offset}" for offset in range(count)]
    await session.execute(
        _BULK_LEAVES,
        {
            "drive_id": drive.id,
            "org_team_id": drive.org_team_id,
            "parent_id": folder.id,
            "depth": folder.depth + 1,
            "ids": ids,
            "inos": [start + offset for offset in range(count)],
            "names": [name.encode() for name in names],
            "displays": names,
            "keys": names,
            "paths": [f"{folder.path_ids}.{ino_label(start + offset)}" for offset in range(count)],
            "acls": [acl_of(offset) for offset in range(count)],
        },
    )
    drive.next_ino = start + count
    await session.commit()
    return ids


async def _access_one_by_one(
    repo: FilesRepo,
    ctx: ActingContext,
    node_ids: Sequence[NodeId],
    facts: AccessFacts,
) -> dict[uuid.UUID, EffectiveAccess]:
    """Each node's access as the route used to compute it: one node, one chain,
    one grant read.

    This is the production per-node path, not a second copy of the batched one.
    """
    resolved: dict[uuid.UUID, EffectiveAccess] = {}
    source = FilesGrantSource()
    for node_id in node_ids:
        node = await repo.node(node_id)
        if node is None:
            continue
        drive = await repo.drive(DriveId(node.drive_id))
        if drive is None:
            continue
        chain = list(await repo.chain(node))
        grants = await source.grants_for(repo, node, chain)
        resolved[node.id] = effective_role(ctx, node, chain, grants, drive, facts)
    return resolved


async def _one_by_one(
    repo: FilesRepo,
    ctx: ActingContext,
    node_ids: Sequence[NodeId],
    facts: AccessFacts,
) -> set[uuid.UUID]:
    """Which of them that path would let this caller read."""
    resolved = await _access_one_by_one(repo, ctx, node_ids, facts)
    return {node_id for node_id, access in resolved.items() if access.allows(FilesAction.READ)}


async def _intern(
    session: AsyncSession, node: FileNode, body: list[dict[str, Any]], org: FilesOrg
) -> None:
    """Give ``node`` a trustworthy interned ACL body, as the rewriter would."""
    acl = FileAcl(
        id=uuid.uuid4(),
        org_team_id=org.org_team_id,
        body=body,
        body_hash=uuid.uuid4().hex * 2,
    )
    session.add(acl)
    await session.flush()
    node.acl_id = acl.id
    await session.commit()


@pytest.mark.parametrize("seed", [1, 7, 23, 101, 4242])
async def test_a_random_page_agrees_with_resolving_each_node_on_its_own(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    seed: int,
) -> None:
    """Over pages built from every candidate rule, the two answers are equal.

    The generator plants what actually moves the decision: direct user grants,
    team grants the caller may or may not hold, an org grant, an expired grant,
    a condition the decider refuses, a held or undownloadable flag, an
    inheritance-only node, and a stale ACL cache — so an agreement here is an
    agreement about the rules, not about an empty page.
    """
    rng = random.Random(seed)
    drive = await files_factory.drive()
    depth = ["f0/", "f0/f1/", "f0/f1/f2/"]
    leaves = [f"f0/f1/f2/n{index}.txt" for index in range(18)]
    made = await files_factory.tree(" ".join([*depth, *leaves]), drive=drive)

    member = files_org.member_id
    now = datetime.now(UTC)
    # Every principal here is one the in-org trigger accepts; what varies is
    # whether it is *this* caller's — the admin's grants and a team the caller
    # does not hold must not reach them.
    principals = [
        ("user", member),
        ("user", files_org.admin_id),
        ("team", files_org.org_team_id),
        ("org", files_org.org_team_id),
    ]
    roles = ["reader", "writer", "manager", "owner"]

    # A grant on a folder is what makes inheritance decide a leaf.
    for folder in depth:
        if rng.random() < 0.5:
            kind, pid = rng.choice(principals)
            await files_factory.grant(made[folder.rstrip("/")], kind, pid, rng.choice(roles))

    for path in leaves:
        node = made[path]
        if rng.random() < 0.6:
            kind, pid = rng.choice(principals)
            share = await files_factory.grant(node, kind, pid, rng.choice(roles))
            if rng.random() < 0.25:
                share.expires_at = now - timedelta(hours=1)
            if rng.random() < 0.2:
                share.conditions = {"team_role": "admin"}
        if rng.random() < 0.25:
            node.flags = rng.choice([NO_DOWNLOAD_BIT, HELD_BIT])
        if rng.random() < 0.45:
            await _intern(
                files_session,
                node,
                [
                    {
                        "principal_kind": "user",
                        "principal_id": str(member),
                        "role": rng.choice(roles),
                    }
                ],
                files_org,
            )
        # Interning first and marking second is what puts a *stale* cache on
        # the page: a body that still names a grant the chain no longer backs.
        if rng.random() < 0.3:
            node.state = ACL_REWRITING
    await files_session.commit()

    facts = AccessFacts(
        team_ids=frozenset({files_org.org_team_id}),
        team_admin_ids=frozenset(),
        now=now,
    )
    ctx = _ctx(files_org, member)
    page = [NodeId(made[path].id) for path in leaves] + [NodeId(uuid.uuid4())]

    async with repo.transaction() as scoped:
        batched = await readable_ids(scoped, ctx, page, facts=facts)
    async with repo.transaction() as scoped:
        reference = await _one_by_one(scoped, ctx, page, facts)

    assert batched == reference
    # An empty answer would agree trivially, so every seed has to decide
    # something; that both outcomes are really discriminated is the next test.
    assert reference, "the seeded page granted the caller nothing at all"
    assert uuid.UUID(int=0) not in batched


async def test_a_page_keeps_the_granted_and_drops_the_ungranted(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """The answer discriminates within one page: a leaf whose grant names the
    caller survives, its sibling whose grant names another member does not, and
    a leaf under a folder granted to the org is reached by inheritance."""
    drive = await files_factory.drive()
    made = await files_factory.tree(
        "plain/ plain/mine.txt plain/theirs.txt plain/bare.txt shared/ shared/inherited.txt",
        drive=drive,
    )
    await files_factory.grant(made["plain/mine.txt"], "user", files_org.member_id, "reader")
    await files_factory.grant(made["plain/theirs.txt"], "user", files_org.admin_id, "owner")
    await files_factory.grant(made["shared"], "org", files_org.org_team_id, "reader")

    ctx = _ctx(files_org, files_org.member_id)
    page = [
        NodeId(made[path].id)
        for path in ("plain/mine.txt", "plain/theirs.txt", "plain/bare.txt", "shared/inherited.txt")
    ]
    async with repo.transaction() as scoped:
        answer = await readable_ids(scoped, ctx, page, facts=AccessFacts())

    assert answer == {made["plain/mine.txt"].id, made["shared/inherited.txt"].id}


async def test_a_child_inherits_an_ancestors_drive_default_grant_from_the_chain(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
) -> None:
    """A node created under a drive-default folder — a home, ``/Shared``, a team
    folder — reads back to the owner of that default even before its own cache
    is materialized.

    A drive-default grant lives only in the interned body of the folder it was
    minted on; it has NO ``file_shares`` row. So a leaf with ``acl_id = NULL``,
    whose grants are read from the chain, must have that ancestor's interned
    body read for its drive-default ACEs — a ``file_shares``-only walk hands the
    member back their own home as if it were empty, which is where every file
    now lives (the root, ``home/`` and ``Teams/`` take no direct write).

    The negative is the point: the owner of the home reaches the leaf, a
    colleague with no rung does not, so the fix is inheriting the grant and not
    opening the container to the org. And the batched answer is checked against
    the per-node path, so a fix to only one of the two would fail here.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("home/ home/mine.txt", drive=drive)
    # The home's `{user: owner}` default, exactly as `default_acl(HOME)` mints
    # it: a drive-default ACE in the interned body, and no `file_shares` row.
    await _intern(
        files_session,
        made["home"],
        [
            {
                "principal_kind": "user",
                "principal_id": str(files_org.member_id),
                "role": "owner",
                "origin": "drive_default",
                "origin_ancestor_id": None,
                "expires_at": None,
                "conditions": None,
            }
        ],
        files_org,
    )
    leaf = [NodeId(made["home/mine.txt"].id)]

    owner = _ctx(files_org, files_org.member_id)
    colleague = _ctx(files_org, uuid.uuid4())
    async with repo.transaction() as scoped:
        for_owner = await readable_ids(scoped, owner, leaf, facts=AccessFacts())
        for_colleague = await readable_ids(scoped, colleague, leaf, facts=AccessFacts())
        owner_one_by_one = await _one_by_one(scoped, owner, leaf, AccessFacts())
        colleague_one_by_one = await _one_by_one(scoped, colleague, leaf, AccessFacts())

    assert for_owner == {made["home/mine.txt"].id}
    assert for_colleague == set()
    # The batched seam and the per-node path agree on both principals.
    assert owner_one_by_one == for_owner
    assert colleague_one_by_one == for_colleague


async def test_an_org_admin_reaches_a_page_with_no_grants_on_it(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """The org-admin descent is the decider's, and the batch keeps it."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt a/c.txt", drive=drive)
    page = [NodeId(made["a/b.txt"].id), NodeId(made["a/c.txt"].id)]
    ctx = _ctx(files_org, files_org.member_id)

    async with repo.transaction() as scoped:
        plain = await readable_ids(scoped, ctx, page, facts=AccessFacts())
        admin = await readable_ids(scoped, ctx, page, facts=AccessFacts(org_admin=True))

    assert plain == set()
    assert admin == set(page)


async def test_a_node_of_another_org_is_absent_not_denied(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_org_factory: Any,
) -> None:
    """A foreign id and a nonexistent id leave the same trace: nothing."""
    stranger = await files_org_factory()
    foreign_drive = await files_factory.drive(org=stranger)
    foreign = (await files_factory.tree("x.txt", drive=foreign_drive))["x.txt"]
    await files_factory.grant(foreign, "org", stranger.org_team_id, "owner")

    ctx = _ctx(files_org, files_org.member_id)
    async with repo.transaction() as scoped:
        answer = await readable_ids(
            scoped,
            ctx,
            [NodeId(foreign.id), NodeId(uuid.uuid4())],
            facts=AccessFacts(
                team_ids=frozenset({files_org.org_team_id, stranger.org_team_id}),
                org_admin=True,
            ),
        )

    assert answer == set()


async def test_a_page_of_ten_thousand_ids_costs_a_constant_number_per_batch(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The budget the seam exists for: cost does not follow the row count.

    A page is resolved a batch of ids at a time — a statement binds a bounded
    number of parameters and a caller's list is not bounded at all — so the
    statements grow with the BATCHES, by a small constant each, and never with
    the rows: a per-node loop would be 10,000 round trips per branch.

    Half the page reads its grants from the interned cache and half from the
    chain, so both loading branches are on the counted path.
    """
    drive = await files_factory.drive()
    folder = (await files_factory.tree("bulk/", drive=drive))["bulk"]
    await files_factory.grant(folder, "org", files_org.org_team_id, "reader")

    acl = FileAcl(
        id=uuid.uuid4(),
        org_team_id=files_org.org_team_id,
        body=[
            {
                "principal_kind": "org",
                "principal_id": str(files_org.org_team_id),
                "role": "reader",
            }
        ],
        body_hash=uuid.uuid4().hex * 2,
    )
    files_session.add(acl)
    await files_session.flush()

    total = 10_000
    made = await _bulk_leaves(
        files_session,
        drive,
        folder,
        total,
        stem="n",
        acl_of=lambda offset: acl.id if offset % 2 == 0 else None,
    )

    page = [NodeId(node_id) for node_id in made]
    ctx = _ctx(files_org, files_org.member_id)
    facts = AccessFacts(team_ids=frozenset({files_org.org_team_id}))

    engine = files_session.get_bind()
    async with repo.transaction() as scoped:
        with counting(engine) as seen:  # type: ignore[arg-type]
            answer = await readable_ids(scoped, ctx, page, facts=facts)

    assert len(answer) == total
    batches = math.ceil(total / ID_BATCH)
    assert len(seen) <= MAX_STATEMENTS * batches, seen


async def test_the_decided_page_carries_the_rows_the_decision_was_made_from(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """``decided_by_id`` hands back each node and its root-first chain, and they
    are the rows the per-node path reads — not a second, racier read of them.

    A surface that renders what it decided (a feed row names its location and
    its path) needs the node and the ancestors beside the answer. If it had to
    fetch them itself it would be back to a statement per row, which is the
    loop the seam deletes.

    The access is compared against the per-node path rather than against
    ``access_by_id``, which now delegates here and would agree by construction.
    The two nodes are granted different rungs so the comparison discriminates:
    an answer that handed every row the same access would pass a check that
    only asked whether the two calls agreed.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("top/ top/mid/ top/mid/leaf.txt other.txt", drive=drive)
    await files_factory.grant(made["top"], "user", files_org.member_id, "reader")
    await files_factory.grant(made["other.txt"], "user", files_org.member_id, "owner")

    ctx = _ctx(files_org, files_org.member_id)
    facts = AccessFacts()
    wanted = [NodeId(made["top/mid/leaf.txt"].id), NodeId(made["other.txt"].id)]
    async with repo.transaction() as scoped:
        decided = await decided_by_id(scoped, ctx, wanted, facts=facts)
        # The reference: the rows and the answer the production per-node path
        # reads, one node at a time.
        chains = {
            node_id: [row.id for row in await scoped.chain(await _node(scoped, node_id))]
            for node_id in wanted
        }
        reference = await _access_one_by_one(scoped, ctx, wanted, facts)

    assert set(decided) == set(wanted)
    for node_id in wanted:
        row = decided[node_id]
        assert row.node.id == node_id
        assert [ancestor.id for ancestor in row.chain] == chains[node_id]
        assert row.access == reference[node_id]
    # The deep leaf's chain really is deeper than the shallow one's, and the
    # two rows really were decided differently, so neither equality above is
    # two empty lists or one answer repeated.
    assert len(decided[wanted[0]].chain) > len(decided[wanted[1]].chain)
    assert decided[wanted[0]].access != decided[wanted[1]].access


async def _node(repo: FilesRepo, node_id: NodeId) -> FileNode:
    node = await repo.node(node_id)
    assert node is not None
    return node


async def test_a_stale_cache_is_ignored_in_favour_of_the_chain(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """While a node is ``acl_rewriting`` the interned body is known wrong, so a
    grant it still names must not be honoured — the same rule
    ``FilesGrantSource`` holds, and the one that would leak a revoked grant."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]
    await _intern(
        files_session,
        node,
        [
            {
                "principal_kind": "user",
                "principal_id": str(files_org.member_id),
                "role": "owner",
            }
        ],
        files_org,
    )
    ctx = _ctx(files_org, files_org.member_id)

    async with repo.transaction() as scoped:
        trusted = await readable_ids(scoped, ctx, [NodeId(node.id)], facts=AccessFacts())

    node.state = ACL_REWRITING
    await files_session.commit()
    async with repo.transaction() as scoped:
        stale = await readable_ids(scoped, ctx, [NodeId(node.id)], facts=AccessFacts())

    assert trusted == {node.id}
    assert stale == set()


async def test_a_frozen_drive_still_reads_but_no_longer_writes(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The drive row rides the same join, so its freeze reaches the decision."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]
    await files_factory.grant(node, "user", files_org.member_id, "owner")
    ctx = _ctx(files_org, files_org.member_id)

    async with repo.transaction() as scoped:
        before = await readable_ids(scoped, ctx, [NodeId(node.id)], facts=AccessFacts())
        writable = await readable_ids(
            scoped, ctx, [NodeId(node.id)], facts=AccessFacts(), action=FilesAction.WRITE
        )

    live = await files_session.get(FileDrive, drive.id)
    assert live is not None
    live.frozen_reason = "over_quota"
    await files_session.commit()

    async with repo.transaction() as scoped:
        frozen_read = await readable_ids(scoped, ctx, [NodeId(node.id)], facts=AccessFacts())
        frozen_write = await readable_ids(
            scoped, ctx, [NodeId(node.id)], facts=AccessFacts(), action=FilesAction.WRITE
        )

    assert before == {node.id}
    assert writable == {node.id}
    assert frozen_read == {node.id}
    assert frozen_write == set()


#: The statement budget for resolving ONE node's effective role, whatever its
#: depth: the node, its drive, its chain, and the chain's grants.
MAX_STATEMENTS_ONE_NODE = 4

#: Deep enough that a per-ancestor read is unmistakable. The performance budget
#: holds path work at depth 256, so that is the depth the grant read is held to as well.
DEEP_CHAIN = 256

#: How many ltree labels a ``file_nodes`` path may keep once this module is
#: done. A GiST index tuple holds the whole value it keys and must fit in one
#: page, so the index over the *full* ``path_ids`` that ``0107``'s
#: ``downgrade()`` rebuilds cannot be built at all while a 256-label path
#: exists — and this lane database is shared with the backend's migration
#: tests, whose downgrade then dies half way down and strands the worker.
DEEPEST_INDEXABLE_PATH = 64


@pytest.fixture
async def the_deep_chain_is_not_left_behind(
    files_session: AsyncSession, files_org: FilesOrg
) -> AsyncIterator[None]:
    """Take the chain back out of the lane database on the way out.

    These sessions commit for real and nothing rolls them back, so a chain this
    deep otherwise belongs to whatever runs next on the worker. Only this
    org's drives are swept: the chain is raw rows nothing else references, but
    a deep folder another module wrote through the services carries history,
    versions and stat deltas that a bare delete trips over, and that residue
    is the migration harness's to clear, with the references in tow.
    """
    yield
    scope = {"depth": DEEPEST_INDEXABLE_PATH, "org": files_org.org_team_id}
    await files_session.execute(
        text("DELETE FROM file_nodes WHERE org_team_id = :org AND nlevel(path_ids) > :depth"),
        scope,
    )
    await files_session.commit()
    deepest = int(
        (
            await files_session.execute(
                text(
                    "SELECT coalesce(max(nlevel(path_ids)), 0) FROM file_nodes"
                    " WHERE org_team_id = :org"
                ),
                scope,
            )
        ).scalar_one()
    )
    await files_session.rollback()
    assert deepest <= DEEPEST_INDEXABLE_PATH, (
        f"a file_nodes path {deepest} labels deep survived this org; the index "
        "a migration downgrade rebuilds cannot be built over it, so the next "
        "migration test on this worker dies half way down"
    )


async def _deep_chain(
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    depth: int,
) -> tuple[FileNode, FileNode]:
    """A single chain ``depth`` nodes long (root included), seeded in one commit.

    Returns the folder just under the root and the leaf, so a test can grant at
    the top and ask at the bottom.
    """
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    parent = await files_session.get(FileNode, drive.root_node_id)
    assert parent is not None
    top: FileNode | None = None
    ino = drive.next_ino
    for index in range(depth - 1):
        node = FileNode(
            id=uuid.uuid4(),
            ino=ino + index,
            drive_id=drive.id,
            org_team_id=files_org.org_team_id,
            parent_id=parent.id,
            kind="folder",
            name=f"d{index}".encode(),
            name_display=f"d{index}",
            name_key=f"d{index}",
            path_ids=f"{parent.path_ids}.{ino_label(ino + index)}",
            depth=parent.depth + 1,
        )
        files_session.add(node)
        if top is None:
            top = node
        parent = node
    drive.next_ino = ino + depth
    await files_session.commit()
    assert top is not None
    return top, parent


async def test_one_deep_nodes_effective_role_does_not_cost_a_statement_per_ancestor(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    the_deep_chain_is_not_left_behind: None,
) -> None:
    """A grant read walks the chain in Python, not in round trips.

    ``FilesGrantSource`` used to issue one ``shares_of`` per ancestor, so a node
    at depth 256 cost 256 statements to answer one access question — the N+1
    the performance budget forbids, and the reason ``shares_of_many`` exists. The answer is
    unchanged: the grant made on the top folder still reaches the leaf by
    inheritance, and it still names that folder as the granting ancestor.
    """
    top, leaf = await _deep_chain(files_factory, files_org, files_session, DEEP_CHAIN)
    await files_factory.grant(top, "user", files_org.member_id, "writer")
    ctx = _ctx(files_org, files_org.member_id)
    engine = files_session.get_bind()

    async with repo.transaction() as scoped:
        chain_len = len(await scoped.chain(leaf))
        with counting(engine) as seen:  # type: ignore[arg-type]
            node = await scoped.node(NodeId(leaf.id))
            assert node is not None
            drive = await scoped.drive(DriveId(node.drive_id))
            assert drive is not None
            chain = list(await scoped.chain(node))
            grants = await FilesGrantSource().grants_for(scoped, node, chain)
            access = effective_role(ctx, node, chain, grants, drive, AccessFacts())

    assert chain_len == DEEP_CHAIN
    assert access.allows(FilesAction.WRITE)
    inherited = [grant for grant in grants if grant.role == "writer"]
    assert [grant.origin.ancestor_id for grant in inherited] == [top.id]
    assert len(seen) <= MAX_STATEMENTS_ONE_NODE, (len(seen), seen[:5])


#: The index a chain lookup addressed by ``(drive_id, ino)`` descends. Named
#: rather than merely "some index": an index over ``drive_id`` alone answers the
#: same rows by reading every node in the drive, which is the cost this pins
#: against.
CHAIN_INDEX: Final = "uq_file_nodes_drive_ino"


#: How many extra siblings the drive holds while the plans below are taken.
#: With a dozen rows in the table every index looks alike to the planner and the
#: assertion would pass for any spelling at all; with a few thousand, reading the
#: whole drive is visibly the expensive plan.
CROWD: Final = 3_000


@asynccontextmanager
async def _a_crowded_drive(
    session: AsyncSession, drive: FileDrive, folder: FileNode
) -> AsyncIterator[None]:
    """Fill ``folder`` with :data:`CROWD` files and refresh the statistics, then
    put both back.

    ``ANALYZE`` is what makes the planner see the rows, and it is also why this
    cleans up: the statistics belong to the whole table, which every other test
    on this worker's database shares, and a neighbour that asserts a plan would
    otherwise be reading this test's row counts.

    Both halves are addressed by the drive's own ino block rather than by three
    thousand ids: the block is contiguous, so it comes back out through
    ``uq_file_nodes_drive_ino`` in one indexed statement.
    """
    start = int(drive.next_ino)
    # Read now: the planning transaction inside ends in a rollback, which
    # expires every instance the session holds.
    drive_id = drive.id
    await _bulk_leaves(session, drive, folder, CROWD, stem="crowd")
    await session.execute(text("ANALYZE file_nodes"))
    await session.commit()
    try:
        yield
    finally:
        await session.execute(
            delete(FileNode).where(
                FileNode.drive_id == drive_id,
                FileNode.ino >= start,
                FileNode.ino < start + CROWD,
            )
        )
        drive.next_ino = start
        await session.commit()
        await session.execute(text("ANALYZE file_nodes"))
        await session.commit()


async def _page_reads(
    repo: FilesRepo,
    ctx: ActingContext,
    page: Sequence[NodeId],
    facts: AccessFacts,
    engine: Engine,
) -> list[tuple[str, Any]]:
    """Every ``file_nodes`` statement one real ``readable_ids`` call issued.

    Captured rather than retyped: what is planned below is the statement the
    page actually runs, so a loader that changes shape is re-examined instead of
    quietly leaving a hand-written twin behind.
    """
    reads: list[tuple[str, Any]] = []

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        executemany: bool,
    ) -> None:
        if not executemany and "file_nodes" in statement:
            reads.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        async with repo.transaction() as scoped:
            await readable_ids(scoped, ctx, page, facts=facts)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return reads


#: The index that answers "every node of the tenant": the policy's own
#: ``org_team_id`` qual is always an index condition on it, so it is a
#: whole-tenant fallback in the same sense a sequential scan is a whole-table one.
TENANT_INDEX: Final = "ix_file_nodes_org_team_id"


class _PlanningRollbackError(Exception):
    """Raised to end the planning transaction in a rollback, never a commit."""


async def _plans_as_the_app_role(repo: FilesRepo, reads: Sequence[tuple[str, Any]]) -> list[str]:
    """``EXPLAIN`` each captured statement inside the Files transaction.

    Which is the whole point: ``repo.transaction()`` assumes
    ``alkera_files_app``, where ``file_nodes`` has FORCE row security, and that
    is the only role under which these plans are the ones production gets.

    The whole-table and whole-tenant fallbacks are taken away because what is
    proven here is which index CAN serve the lookup under the role, not which
    plan this worker's statistics happen to prefer. The statistics are the whole
    table's, and every other test on the worker's database leaves a small tree
    whose inos are 1, 2, 3...: ``ino = ANY('{1..12}')`` then reads as a large
    slice of the table, and the tenant index — driven by the policy's
    ``org_team_id`` qual, which it can always serve — came out cheaper than the
    chain index on some workers and not others. The sequential and bitmap scans
    are switched off; the tenant index is dropped inside this transaction, which
    is then rolled back, so no other statement ever sees it gone. With those
    gone every remaining candidate that the query can drive leads with
    ``drive_id``, and among them the one that also takes ``ino`` as an index
    condition is the cheapest at any row count.

    None of this can rescue the ltree spelling: a qual the planner may not
    evaluate before the policy's own is not an index condition at any row
    count, so it stays a filter over whichever index is scanned instead.
    """
    plans: list[str] = []
    with contextlib.suppress(_PlanningRollbackError):
        async with repo.transaction() as scoped:
            connection = await scoped.session.connection()
            # The session user owns the table; the role is assumed again below,
            # so every EXPLAIN still runs under the policy.
            await connection.exec_driver_sql("SET LOCAL ROLE NONE")
            await connection.exec_driver_sql(f"DROP INDEX {TENANT_INDEX}")
            await connection.exec_driver_sql(f"SET LOCAL ROLE {APP_ROLE}")
            await connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
            await connection.exec_driver_sql("SET LOCAL enable_bitmapscan = off")
            for statement, parameters in reads:
                rows = await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)
                plans.append("\n".join(str(row[0]) for row in rows.fetchall()))
            raise _PlanningRollbackError
    return plans


def _chain_index_conditions(plan: str) -> list[str]:
    """The ``Index Cond`` of every scan of :data:`CHAIN_INDEX` in ``plan``."""
    lines = plan.splitlines()
    return [
        following.strip()
        for index, line in enumerate(lines)
        if f"using {CHAIN_INDEX} " in f"{line} "
        for following in lines[index + 1 : index + 2]
        if "Index Cond:" in following
    ]


async def test_the_page_reaches_its_ancestors_through_an_index_under_the_app_role(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
) -> None:
    """The chain lookup is index-served as the role the routes actually run as.

    Planning it as the table owner proves nothing about production. Under
    ``alkera_files_app`` the policy on ``file_nodes`` is FORCE'd, and PostgreSQL
    will not evaluate a qual ahead of a pending security qual unless that qual
    is leakproof — ``subpath()`` and the ltree containment operators are not, so
    however they are spelled they are demoted to a filter and the planner's only
    remaining handle is ``drive_id``: a read of every node in the drive, once
    per item of the page. Addressing the chain by ``(drive_id, ino)`` instead
    asks with ``uuid_eq`` and ``int8eq``, which are leakproof, so the unique
    index still answers under the role.

    Both halves matter. An ltree qual left anywhere in these plans is the
    demotion coming back; the named index is what replaced it.
    """
    drive = await files_factory.drive()
    leaves = [f"f0/f1/f2/n{index}.txt" for index in range(8)]
    made = await files_factory.tree(" ".join(["f0/", "f0/f1/", "f0/f1/f2/", *leaves]), drive=drive)
    await files_factory.grant(made["f0"], "user", files_org.member_id, "reader")

    ctx = _ctx(files_org, files_org.member_id)
    facts = AccessFacts(team_ids=frozenset({files_org.org_team_id}))
    engine = files_session.get_bind()

    async with _a_crowded_drive(files_session, drive, made["f0"]):
        reads = await _page_reads(
            repo, ctx, [NodeId(made[path].id) for path in leaves], facts, engine
        )
        plans = await _plans_as_the_app_role(repo, reads)

    assert plans, "the page ran no file_nodes statement at all"
    ltree = [plan for plan in plans if "path_ids" in plan]
    assert ltree == [], (
        "an ltree predicate survived into the plan under the app role, where it "
        f"cannot be an index condition: {ltree}"
    )
    # ``ino`` must be in the index condition, not merely the index named: with
    # only ``drive_id`` usable, the chain index is one more way to read the drive.
    conditions = [cond for plan in plans for cond in _chain_index_conditions(plan)]
    assert any("ino" in cond for cond in conditions), (
        f"no statement of the page reached {CHAIN_INDEX} by (drive_id, ino); the "
        f"ancestors are being found by scanning the drive: {plans}"
    )

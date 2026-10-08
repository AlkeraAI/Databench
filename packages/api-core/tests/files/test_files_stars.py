"""Starring: a compare-and-swap bit, announced once and only when it moved."""

from __future__ import annotations

import uuid

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.events.types import EventType
from alkera_core.files import stars
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.filters import ListFilters
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.event_outbox import EventOutbox
from alkera_core.models.files.history import FileHistory
from alkera_core.models.files.tree import FileNode
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
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


async def _history_count(repo: FilesRepo, node_id: uuid.UUID) -> int:
    counted = await repo.execute_scoped(
        select(func.count()).select_from(FileHistory).where(FileHistory.node_id == node_id)
    )
    return int(counted.scalar_one())


async def _outbox_count(repo: FilesRepo, node_id: uuid.UUID) -> int:
    """Read the outbox outside the Files role, which cannot see that table."""
    counted = await repo.session.execute(
        select(func.count())
        .select_from(EventOutbox)
        .where(
            EventOutbox.entity_id == str(node_id),
            EventOutbox.type == EventType.FILE_NODE_CHANGED,
        )
    )
    return int(counted.scalar_one())


async def test_starring_sets_the_bit_and_writes_history_and_an_outbox_row(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("doc.txt", drive=drive)
    node_id = NodeId(nodes["doc.txt"].id)
    async with repo.transaction():
        assert await stars.star(repo, _ctx(files_org), node_id) is True
    async with repo.transaction():
        row = await repo.node(node_id)
        assert row is not None
        assert stars.star_bit_of(row.flags)
        assert await _history_count(repo, node_id) == 1
    assert await _outbox_count(repo, node_id) == 1


async def test_starring_twice_is_a_no_op_that_announces_nothing(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    """A second star did not change the node, so it must not claim it did."""
    drive = await files_factory.drive()
    nodes = await files_factory.tree("doc.txt", drive=drive)
    node_id = NodeId(nodes["doc.txt"].id)
    async with repo.transaction():
        await stars.star(repo, _ctx(files_org), node_id)
    async with repo.transaction():
        assert await stars.star(repo, _ctx(files_org), node_id) is False
    async with repo.transaction():
        assert await _history_count(repo, node_id) == 1
    assert await _outbox_count(repo, node_id) == 1


async def test_unstarring_clears_only_the_star_bit(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("doc.txt", drive=drive)
    node_id = NodeId(nodes["doc.txt"].id)
    other_bit = 1 << 4
    async with repo.transaction():
        await repo.session.execute(
            repo.update_nodes().where(FileNode.id == node_id).values(flags=other_bit)
        )
        await stars.star(repo, _ctx(files_org), node_id)
        assert await stars.unstar(repo, _ctx(files_org), node_id) is True
        row = await repo.node(node_id)
        assert row is not None
    # The neighbouring bit survived both writes: the update is a mask, not a set.
    assert row.flags == other_bit
    assert not stars.star_bit_of(row.flags)


async def test_unstarring_something_that_was_never_starred_announces_nothing(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("doc.txt", drive=drive)
    node_id = NodeId(nodes["doc.txt"].id)
    async with repo.transaction():
        assert await stars.unstar(repo, _ctx(files_org), node_id) is False
        assert await _history_count(repo, node_id) == 0


@pytest.mark.parametrize(
    "action",
    [pytest.param(stars.star, id="star"), pytest.param(stars.unstar, id="unstar")],
)
async def test_a_node_in_another_org_is_not_found(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, action: object
) -> None:
    await files_factory.drive()
    async with repo.transaction():
        with pytest.raises(NotFound):
            await action(repo, _ctx(files_org), NodeId(uuid.uuid4()))  # type: ignore[operator]


async def test_a_trashed_node_cannot_be_starred(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("doc.txt", drive=drive)
    node_id = NodeId(nodes["doc.txt"].id)
    async with repo.transaction():
        await repo.session.execute(
            repo.update_nodes().where(FileNode.id == node_id).values(trashed_at=func.now())
        )
        # The row exists and is readable, so this is the no-op answer, not a 404.
        assert await stars.star(repo, _ctx(files_org), node_id) is False
        row = await repo.node(node_id)
        assert row is not None
    assert not stars.star_bit_of(row.flags)


async def test_starred_lists_only_the_starred_and_respects_a_chip(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("a.txt b.txt folder/", drive=drive)
    async with repo.transaction():
        for path in ("a.txt", "folder"):
            await stars.star(repo, _ctx(files_org), NodeId(nodes[path].id))
    async with repo.transaction():
        page = await stars.starred(repo, _ctx(files_org))
        only_files = await stars.starred(repo, _ctx(files_org), filters=ListFilters(kind="file"))
    assert {row.id for row in page.items} == {nodes["a.txt"].id, nodes["folder"].id}
    assert [row.id for row in only_files.items] == [nodes["a.txt"].id]


async def test_an_unreadable_star_never_shortens_the_page(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    # The hidden one sorts first, so a page cut before the readability filter
    # would carry it and the assertions could not pass by luck.
    nodes = await files_factory.tree("s2.txt s3.txt s4.txt s1-secret.txt", drive=drive)
    hidden = nodes["s1-secret.txt"].id
    async with repo.transaction():
        for node in nodes.values():
            await stars.star(repo, _ctx(files_org), NodeId(node.id))

    def hides_the_secret(_table: object) -> object:
        return FileNode.id != hidden

    async with repo.transaction():
        plain = await stars.starred(repo, _ctx(files_org), limit=2)
        filtered = await stars.starred(
            repo,
            _ctx(files_org),
            limit=2,
            readable_predicate=hides_the_secret,  # type: ignore[arg-type]
        )
    assert hidden in {row.id for row in plain.items}
    assert hidden not in {row.id for row in filtered.items}
    assert [row.name_key for row in filtered.items] == ["s2.txt", "s3.txt"]
    assert len(plain.items) == len(filtered.items) == 2
    assert plain.has_more is filtered.has_more is True


async def test_a_starred_marker_cannot_be_replayed_against_search(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    from alkera_core.files import search

    drive = await files_factory.drive()
    nodes = await files_factory.tree("s1.txt s2.txt s3.txt", drive=drive)
    async with repo.transaction():
        for node in nodes.values():
            await stars.star(repo, _ctx(files_org), NodeId(node.id))
    async with repo.transaction():
        page = await stars.starred(repo, _ctx(files_org), limit=1)
        assert page.next_marker is not None
        with pytest.raises(InvalidRequest):
            await search.search_names(
                repo, _ctx(files_org), DriveId(drive.id), "s", marker=page.next_marker
            )


# --- the star on the wire: per caller, and inside the listing's budget -------


def _ctx_for(org: FilesOrg, user_id: uuid.UUID) -> ActingContext:
    """The same org, a different person — the second half of "per user"."""
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(user_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def test_a_listing_carries_the_star_for_its_caller_and_not_for_anyone_else(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    """A star is one person's bookmark, so the page answers per caller.

    The node bit `starred()` filters on says only that *someone* starred the
    node; if the wire read that bit instead of the caller's own row, the
    stranger's page below would carry the star too.
    """
    from alkera_core.files.listing import children

    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    nodes = await files_factory.tree("mine.txt theirs.txt plain.txt", drive=drive)
    stranger = uuid.uuid4()

    async with repo.transaction():
        await stars.star(repo, _ctx(files_org), NodeId(nodes["mine.txt"].id))
    async with repo.transaction():
        await stars.star(repo, _ctx_for(files_org, stranger), NodeId(nodes["theirs.txt"].id))

    async with repo.transaction():
        ours = await children(repo, _ctx(files_org), drive.root_node_id, limit=10)
    async with repo.transaction():
        theirs = await children(repo, _ctx_for(files_org, stranger), drive.root_node_id, limit=10)

    assert ours.starred_ids == {nodes["mine.txt"].id}
    assert theirs.starred_ids == {nodes["theirs.txt"].id}
    # Both pages carry all three rows: a star narrows nothing, it only marks.
    assert len(ours.items) == len(theirs.items) == 3


async def test_an_unstar_clears_only_the_callers_own_star(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    """Two people star one node; one unstars. The other still sees their star."""
    from alkera_core.files.listing import children

    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    nodes = await files_factory.tree("shared.txt", drive=drive)
    node_id = NodeId(nodes["shared.txt"].id)
    other = uuid.uuid4()

    async with repo.transaction():
        await stars.star(repo, _ctx(files_org), node_id)
    async with repo.transaction():
        await stars.star(repo, _ctx_for(files_org, other), node_id)
    async with repo.transaction():
        await stars.unstar(repo, _ctx(files_org), node_id)

    async with repo.transaction():
        ours = await children(repo, _ctx(files_org), drive.root_node_id, limit=10)
    async with repo.transaction():
        theirs = await children(repo, _ctx_for(files_org, other), drive.root_node_id, limit=10)

    assert ours.starred_ids == frozenset()
    assert theirs.starred_ids == {nodes["shared.txt"].id}


async def test_a_principal_with_no_user_behind_it_stars_nothing_on_the_wire(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    """An agent has no bookmarks list, so no page carries its star."""
    from alkera_core.files.listing import children

    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    nodes = await files_factory.tree("a.txt", drive=drive)
    agent = ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.AGENT,
            id=str(uuid.uuid4()),
            org_id=files_org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )
    async with repo.transaction():
        assert await stars.star(repo, agent, NodeId(nodes["a.txt"].id)) is True
    async with repo.transaction():
        page = await children(repo, agent, drive.root_node_id, limit=10)
        by_user = await children(repo, _ctx(files_org), drive.root_node_id, limit=10)

    assert page.starred_ids == frozenset()
    assert by_user.starred_ids == frozenset()
    # The node bit moved all the same, so the `starred=` chip still finds it.
    assert stars.star_bit_of(page.items[0].flags) is True


async def test_the_caller_star_rides_the_rows_statement_and_costs_no_extra_query(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    files_session: AsyncSession,
) -> None:
    """The listing budget is three statements; the star does not buy a fourth.

    A page of ten starred rows would be ten extra reads if the star were looked
    up per item, and one extra read if it were a second `IN (…)` pass. Both show
    up here as a fourth counted statement.
    """
    from alkera_core.files.listing import children

    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree(" ".join(f"f{n:02d}.txt" for n in range(10)), drive=drive)
    async with repo.transaction():
        for node in made.values():
            await stars.star(repo, _ctx(files_org), NodeId(node.id))

    counted: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "FROM file_" in statement:
            counted.append(statement)

    engine = files_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with repo.transaction():
            page = await children(repo, _ctx(files_org), drive.root_node_id, limit=10)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert page.starred_ids == {node.id for node in made.values()}
    assert len(counted) <= 3, "\n---\n".join(counted)


def _ctx_for(org: FilesOrg, user_id: uuid.UUID) -> ActingContext:
    """A second real member of the same org, so "the caller" is not always one."""
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(user_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def test_every_keyset_feed_renders_the_callers_star_and_not_a_colleagues(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
) -> None:
    """Search, recent, sharedWithMe and the starred feed are per caller.

    Each feed builds its page through `keyset_page`, so each one used to render
    the node bit — "somebody starred this" — and a colleague's bookmark showed
    up as the caller's own star.
    """
    from alkera_core.files import search as search_lib

    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    nodes = await files_factory.tree("mine.txt theirs.txt", drive=drive)
    mine, theirs = nodes["mine.txt"], nodes["theirs.txt"]
    me = _ctx(files_org)
    them = _ctx_for(files_org, files_org.member_id)

    async with repo.transaction():
        assert await stars.star(repo, me, NodeId(mine.id)) is True
        assert await stars.star(repo, them, NodeId(theirs.id)) is True
        # A star and an immediate unstar leaves history on `theirs` for me — so
        # it lands in *my* recent while carrying only their star.
        await stars.star(repo, me, NodeId(theirs.id))
        await stars.unstar(repo, me, NodeId(theirs.id))
    await files_factory.grant(mine, "user", files_org.admin_id, "reader")
    await files_factory.grant(theirs, "user", files_org.admin_id, "reader")

    async with repo.transaction():
        found = await search_lib.search_names(repo, me, DriveId(drive.id), "txt", limit=10)
        found_theirs = await search_lib.search_names(repo, them, DriveId(drive.id), "txt", limit=10)
        seen = await search_lib.recent(repo, me, limit=10)
        shared = await search_lib.shared_with_me(repo, me, limit=10)
        starred_feed = await stars.starred(repo, me, limit=10)
        their_feed = await stars.starred(repo, them, limit=10)

    assert {row.id for row in found.items} == {mine.id, theirs.id}
    assert found.starred_ids == {mine.id}
    assert found_theirs.starred_ids == {theirs.id}
    # Both nodes are in my recent; only one of them is mine to star.
    assert {row.id for row in seen.items} == {mine.id, theirs.id}
    assert seen.starred_ids == {mine.id}
    assert {row.id for row in shared.items} == {mine.id, theirs.id}
    assert shared.starred_ids == {mine.id}
    # The feed itself is the caller's bookmarks, not every starred node.
    assert [row.id for row in starred_feed.items] == [mine.id]
    assert starred_feed.starred_ids == {mine.id}
    assert [row.id for row in their_feed.items] == [theirs.id]


async def test_the_starred_chip_narrows_to_the_callers_own_stars(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
) -> None:
    """`starred=true` is one person's bookmarks, on every surface that takes it."""
    from alkera_core.files.listing import children

    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    nodes = await files_factory.tree("mine.txt theirs.txt", drive=drive)
    mine, theirs = nodes["mine.txt"], nodes["theirs.txt"]
    me = _ctx(files_org)
    them = _ctx_for(files_org, files_org.member_id)
    async with repo.transaction():
        await stars.star(repo, me, NodeId(mine.id))
        await stars.star(repo, them, NodeId(theirs.id))

    parsed = ListFilters.from_query({"starred": "true"}, me=files_org.admin_id)
    async with repo.transaction():
        page = await children(repo, me, drive.root_node_id, filters=parsed, limit=10)
        theirs_page = await children(
            repo,
            them,
            drive.root_node_id,
            filters=ListFilters(starred=True, caller=files_org.member_id),
            limit=10,
        )
        without = await children(
            repo,
            me,
            drive.root_node_id,
            filters=ListFilters(starred=False, caller=files_org.admin_id),
            limit=10,
        )

    assert [row.id for row in page.items] == [mine.id]
    assert [row.id for row in theirs_page.items] == [theirs.id]
    # The negative chip is its complement, not "nobody starred it".
    assert [row.id for row in without.items] == [theirs.id]


async def test_a_feed_page_spends_no_statement_on_the_star(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    files_session: AsyncSession,
) -> None:
    """`keyset_page` is the rows statement plus the ACL one — the star buys neither."""
    from alkera_core.files import search as search_lib

    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree(" ".join(f"g{n:02d}.txt" for n in range(10)), drive=drive)
    me = _ctx(files_org)
    async with repo.transaction():
        for node in made.values():
            await stars.star(repo, me, NodeId(node.id))

    counted: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "FROM file_" in statement:
            counted.append(statement)

    engine = files_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with repo.transaction():
            page = await search_lib.search_names(repo, me, DriveId(drive.id), "txt", limit=10)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert page.starred_ids == {node.id for node in made.values()}
    assert len(counted) <= 2, "\n---\n".join(counted)

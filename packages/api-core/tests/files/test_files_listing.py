"""The listing page: its marker contract, its filters, and its query budget.

Every test here runs against real Postgres through `FilesRepo`, because the
claims are about what SQL does — a keyset resume across a concurrent rename, a
predicate that excludes the planted non-match, a page that costs three
statements — and none of them survive being mocked.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.authz.principal import ActingContext, Principal, PrincipalKind
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.filters import (
    FLAG_STARRED,
    Direction,
    ListFilters,
    Marker,
    MarkerInvalid,
    OrderBy,
    OrderField,
)
from alkera_core.files.listing import MAX_LIMIT, Page, children
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.acl import FileAcl
from alkera_core.models.files.history import FileStar
from alkera_core.models.files.leases import FileLease
from alkera_core.models.files.tree import FileNode
from sqlalchemy import delete, event, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files.concurrency import conftest as _concurrency

#: A second real backend, so a concurrency claim is about two connections.
concurrency_engine = _concurrency.concurrency_engine
sessions = _concurrency.sessions


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER, id=str(org.member_id), org_id=org.org_team_id
        )
    )


async def _page_all(
    repo: FilesRepo,
    ctx: ActingContext,
    parent_id: uuid.UUID,
    *,
    limit: int,
    order_by: OrderBy | None = None,
    filters: ListFilters | None = None,
    between_pages: Callable[[int], Awaitable[None]] | None = None,
) -> list[FileNode]:
    """Enumerate every page, optionally letting a caller churn between pages."""
    seen: list[FileNode] = []
    marker: Marker | None = None
    index = 0
    while True:
        async with repo.transaction():
            page = await children(
                repo,
                ctx,
                parent_id,
                order_by=order_by,
                filters=filters,
                marker=marker,
                limit=limit,
            )
        seen.extend(page.items)
        marker = page.next_marker
        index += 1
        if marker is None:
            return seen
        if between_pages is not None:
            await between_pages(index)


# ---------------------------------------------------------------------------
# The marker contract
# ---------------------------------------------------------------------------


async def test_marker_contract_holds_while_a_second_session_churns_the_folder(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    sessions: Any,
) -> None:
    """Every entry unchanged for the whole enumeration appears exactly once.

    An entry renamed, added or deleted mid-enumeration may appear zero, once or
    twice — that is the published contract — but it may never displace an
    untouched sibling or corrupt a page.
    """
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    total = 5000
    spec = " ".join(f"f{n:05d}.txt" for n in range(total))
    made = await files_factory.tree(spec, drive=drive)
    root_id = drive.root_node_id

    # The churn set is disjoint from the unchanged set, so "exactly once" is a
    # claim about rows the writer never touches.
    renamed = [made[f"f{n:05d}.txt"] for n in range(0, 400, 40)]
    deleted = [made[f"f{n:05d}.txt"] for n in range(1, 400, 40)]
    churned = {row.id for row in renamed} | {row.id for row in deleted}
    unchanged = {row.id for row in made.values()} - churned

    (writer,) = await sessions(1)
    added: list[uuid.UUID] = []
    next_ino = drive.next_ino + 1000

    async def churn(page_index: int) -> None:
        """One rename, one delete and one insert between two pages."""
        step = page_index - 1
        if step >= len(renamed):
            return
        moved = renamed[step]
        # A rename that jumps the row to the end of the name order: the row may
        # be seen twice (once at its old key, once at its new one) and that is
        # allowed; nothing else may shift.
        await writer.execute(
            update(FileNode)
            .where(FileNode.id == moved.id)
            .values(name=b"zzz-" + moved.name, name_key=f"zzz-{moved.name.decode()}")
        )
        gone = deleted[step]
        await writer.execute(
            update(FileNode).where(FileNode.id == gone.id).values(trashed_at=datetime.now(tz=UTC))
        )
        fresh = uuid.uuid4()
        nonlocal next_ino
        next_ino += 1
        await writer.execute(
            insert(FileNode).values(
                id=fresh,
                ino=next_ino,
                drive_id=drive.id,
                org_team_id=files_org.org_team_id,
                parent_id=root_id,
                kind="file",
                name=f"new-{step:03d}.txt".encode(),
                name_display=f"new-{step:03d}.txt",
                name_key=f"new-{step:03d}.txt",
                path_ids=f"{ino_label(1)}.{ino_label(next_ino)}",
                depth=1,
            )
        )
        await writer.commit()
        added.append(fresh)

    seen = await _page_all(repo, _ctx(files_org), root_id, limit=97, between_pages=churn)

    counts: dict[uuid.UUID, int] = {}
    for row in seen:
        counts[row.id] = counts.get(row.id, 0) + 1

    missed = sorted(id for id in unchanged if counts.get(id, 0) != 1)
    assert not missed, f"{len(missed)} untouched rows did not appear exactly once"
    for id in churned | set(added):
        assert counts.get(id, 0) <= 2, "a churned row appeared more than twice"
    # The enumeration really did span the churn: the writer committed on every
    # page boundary, so a run that saw one page proves nothing.
    assert len(added) >= len(renamed)


@pytest.mark.parametrize(
    ("field", "direction"),
    [
        pytest.param(field, direction, id=f"{field.value}-{direction.value}")
        for field in OrderField
        for direction in Direction
    ],
)
async def test_every_order_is_total_and_stable_across_pages(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    field: OrderField,
    direction: Direction,
) -> None:
    """Paging in any order yields the same sequence a single big page does.

    Sizes, mtimes and kinds are planted with deliberate ties so the id
    tie-break is what makes the order total; without it a page boundary inside
    a tie group drops or repeats rows.
    """
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree(
        " ".join(f"n{n:03d}" + ("/" if n % 3 == 0 else ".txt") for n in range(60)), drive=drive
    )
    for index, row in enumerate(made.values()):
        row.size = (index % 5) * 100
        row.mtime_ns = (index % 4) * 1_000_000_000
    await files_session.commit()

    order = OrderBy(field=field, direction=direction)
    paged = await _page_all(repo, _ctx(files_org), drive.root_node_id, limit=7, order_by=order)
    whole = await _page_all(
        repo, _ctx(files_org), drive.root_node_id, limit=MAX_LIMIT, order_by=order
    )

    assert [row.id for row in paged] == [row.id for row in whole]
    assert len({row.id for row in paged}) == len(made)


async def test_a_tampered_marker_is_refused(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """A cursor is client-held state, so it is signed and verified."""
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    await files_factory.tree("a.txt b.txt c.txt", drive=drive)
    order = OrderBy()
    async with repo.transaction():
        page = await children(repo, _ctx(files_org), drive.root_node_id, limit=2)
    assert page.next_marker is not None
    raw = page.next_marker.encode()

    # Round-trip first: the honest cursor decodes to the same position.
    back = Marker.decode(raw, parent_id=drive.root_node_id, order=order)
    assert back == page.next_marker

    head, _, tail = raw.partition(".")
    forged = Marker(
        parent_id=drive.root_node_id, order=order, value="zzzz", last_id=uuid.uuid4()
    ).encode()
    with pytest.raises(MarkerInvalid):
        # body swapped, signature kept
        Marker.decode(
            f"{forged.partition('.')[0]}.{tail}", parent_id=drive.root_node_id, order=order
        )
    with pytest.raises(MarkerInvalid):
        Marker.decode(head, parent_id=drive.root_node_id, order=order)
    with pytest.raises(MarkerInvalid):
        Marker.decode(raw, parent_id=uuid.uuid4(), order=order)
    with pytest.raises(MarkerInvalid):
        Marker.decode(raw, parent_id=drive.root_node_id, order=OrderBy(direction=Direction.DESC))


# ---------------------------------------------------------------------------
# The filters
# ---------------------------------------------------------------------------


def _match_and_miss(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(values that must match, values that must not) for one filter."""
    table: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
        "kind": ({"kind": "symlink"}, {"kind": "file"}),
        "object_type": ({"subtype": "fifo"}, {"subtype": "socket"}),
        "mime_class": ({"mime_class": "image"}, {"mime_class": "code"}),
        "modified_after": ({"mtime_ns": 4_000_000_000}, {"mtime_ns": 1_000_000_000}),
        "modified_before": ({"mtime_ns": 1_000_000_000}, {"mtime_ns": 4_000_000_000}),
        "size_min": ({"size": 5000}, {"size": 10}),
        "size_max": ({"size": 10}, {"size": 5000}),
        "name_flag": (
            {"flags_names": {"windows_safe": True}},
            {"flags_names": {"windows_safe": False}},
        ),
        # Both twins carry the node bit — "somebody starred this" — so only the
        # caller's own star row can be what the chip selects on.
        "starred": ({"flags": FLAG_STARRED}, {"flags": FLAG_STARRED}),
    }
    return table[name]


@pytest.mark.parametrize(
    ("filters", "column_case"),
    [
        pytest.param(ListFilters(kind="symlink"), "kind", id="kind"),
        pytest.param(ListFilters(object_type="fifo"), "object_type", id="object-type"),
        pytest.param(ListFilters(mime_class="image"), "mime_class", id="mime-class"),
        pytest.param(
            ListFilters(modified_after=datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC)),
            "modified_after",
            id="modified-after",
        ),
        pytest.param(
            ListFilters(modified_before=datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC)),
            "modified_before",
            id="modified-before",
        ),
        pytest.param(ListFilters(size_min=1000), "size_min", id="size-min"),
        pytest.param(ListFilters(size_max=1000), "size_max", id="size-max"),
        pytest.param(ListFilters(name_flag="windows_safe"), "name_flag", id="name-flag"),
        pytest.param(ListFilters(starred=True), "starred", id="starred"),
    ],
)
async def test_each_filter_keeps_the_planted_match_and_drops_its_twin(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    filters: ListFilters,
    column_case: str,
) -> None:
    """Every chip is a SQL predicate: one planted match in, one planted miss out."""
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree("hit.txt miss.txt", drive=drive)
    match_values, miss_values = _match_and_miss(column_case)
    # `modifiedAfter` is >= a nanosecond epoch offset; the timestamps above are
    # three seconds past the factory epoch, which the ns values straddle.
    if column_case in {"modified_after", "modified_before"}:
        base = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1_000_000_000)
        match_values = {"mtime_ns": base + match_values["mtime_ns"]}
        miss_values = {"mtime_ns": base + miss_values["mtime_ns"]}
    for key, value in match_values.items():
        setattr(made["hit.txt"], key, value)
    for key, value in miss_values.items():
        setattr(made["miss.txt"], key, value)
    if column_case == "starred":
        # A star is one person's bookmark, so the chip needs a caller to mean
        # anything: the twin that is not theirs must drop out.
        filters = ListFilters(starred=True, caller=files_org.admin_id)
        files_session.add(
            FileStar(
                org_team_id=files_org.org_team_id,
                user_id=files_org.admin_id,
                node_id=made["hit.txt"].id,
            )
        )
    await files_session.commit()

    async with repo.transaction():
        page = await children(repo, _ctx(files_org), drive.root_node_id, filters=filters, limit=50)
    assert [row.id for row in page.items] == [made["hit.txt"].id]


@pytest.mark.parametrize("wanted", [True, False], ids=["shared", "not-shared"])
async def test_the_shared_filter_reads_live_grants_only(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    wanted: bool,
) -> None:
    """A revoked grant does not make a node shared — the negative twin of the chip."""
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree("live.txt revoked.txt plain.txt", drive=drive)
    await files_factory.grant(made["live.txt"], "user", files_org.member_id, "reader")
    revoked = await files_factory.grant(made["revoked.txt"], "user", files_org.member_id, "reader")
    revoked.revoked_at = datetime.now(tz=UTC)
    await files_session.commit()

    async with repo.transaction():
        page = await children(
            repo,
            _ctx(files_org),
            drive.root_node_id,
            filters=ListFilters(shared=wanted),
            limit=50,
        )
    got = {row.id for row in page.items}
    if wanted:
        assert got == {made["live.txt"].id}
    else:
        assert got == {made["revoked.txt"].id, made["plain.txt"].id}


@pytest.mark.parametrize("wanted", [True, False], ids=["leased", "not-leased"])
async def test_the_leased_filter_reads_unreleased_leases_only(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    wanted: bool,
) -> None:
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree("held/ released/ free/", drive=drive)
    now = datetime.now(tz=UTC)
    for key, released in (("held/", None), ("released/", now)):
        files_session.add(
            FileLease(
                node_id=made[key.rstrip("/")].id,
                org_team_id=files_org.org_team_id,
                epoch=1,
                holder_principal_kind="user",
                holder_principal_id=files_org.member_id,
                holder_instance_id="i",
                machine_id="m",
                purpose="mount",
                expires_at=now + timedelta(minutes=5),
                grantable_after=now,
                released_at=released,
            )
        )
    await files_session.commit()

    async with repo.transaction():
        page = await children(
            repo,
            _ctx(files_org),
            drive.root_node_id,
            filters=ListFilters(leased=wanted),
            limit=50,
        )
    got = {row.id for row in page.items}
    if wanted:
        assert got == {made["held"].id}
    else:
        assert got == {made["released"].id, made["free"].id}


async def test_the_owner_filter_resolves_me_to_the_caller(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
) -> None:
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree("mine.txt theirs.txt", drive=drive)
    made["mine.txt"].created_by = files_org.member_id
    made["theirs.txt"].created_by = files_org.admin_id
    await files_session.commit()

    filters = ListFilters.from_query({"owner": "me"}, me=files_org.member_id)
    assert filters.owner == files_org.member_id
    async with repo.transaction():
        page = await children(repo, _ctx(files_org), drive.root_node_id, filters=filters, limit=50)
    assert [row.id for row in page.items] == [made["mine.txt"].id]


async def test_a_listing_is_live_rows_unless_the_trash_view_is_asked_for(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
) -> None:
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree("kept.txt gone.txt", drive=drive)
    made["gone.txt"].trashed_at = datetime.now(tz=UTC)
    await files_session.commit()

    async with repo.transaction():
        live = await children(repo, _ctx(files_org), drive.root_node_id, limit=50)
        trashed = await children(
            repo,
            _ctx(files_org),
            drive.root_node_id,
            filters=ListFilters(trashed=True),
            limit=50,
        )
    assert [row.id for row in live.items] == [made["kept.txt"].id]
    assert [row.id for row in trashed.items] == [made["gone.txt"].id]


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        pytest.param({"sizeMin": "banana"}, "not a size", id="size-not-a-number"),
        pytest.param({"sizeMin": "-1"}, "negative size", id="size-negative"),
        pytest.param({"sizeMin": "10", "sizeMax": "5"}, "greater than", id="size-inverted"),
        pytest.param({"nameFlag": "linux_safe"}, "not a name flag", id="unknown-name-flag"),
        pytest.param({"sparkles": "true"}, "unknown filter", id="unknown-key"),
        pytest.param({"starred": "maybe"}, "not a boolean", id="bad-boolean"),
        pytest.param({"mimeClass": "hologram"}, "not a mime class", id="bad-mime-class"),
        pytest.param({"kind": "wormhole"}, "not a kind", id="bad-kind"),
        pytest.param({"owner": "nobody"}, "not a principal id", id="bad-owner"),
        pytest.param({"modifiedAfter": "yesterday"}, "not a timestamp", id="bad-time"),
    ],
)
def test_from_query_refuses_what_it_cannot_represent(params: dict[str, str], reason: str) -> None:
    """A bad or unknown filter is a 422, never a silently unfiltered page."""
    with pytest.raises(InvalidRequest) as caught:
        ListFilters.from_query(params, me=uuid.uuid4())
    assert reason in str(caught.value)


def test_from_query_accepts_the_whole_chip_set() -> None:
    """The positive twin: every published key parses into a typed field."""
    me = uuid.uuid4()
    filters = ListFilters.from_query(
        {
            "kind": "file",
            "objectType": "fifo",
            "mimeClass": "image",
            "owner": "me",
            "modifiedAfter": "2026-01-01T00:00:00+00:00",
            "modifiedBefore": "2026-02-01T00:00:00+00:00",
            "sizeMin": "0",
            "sizeMax": "1024",
            "nameFlag": "macos_safe",
            "starred": "true",
            "shared": "false",
            "leased": "1",
            "trashed": "no",
        },
        me=me,
    )
    assert filters.owner == me
    assert filters.size_min == 0 and filters.size_max == 1024
    assert filters.starred is True and filters.shared is False
    assert filters.leased is True and filters.trashed is False
    assert filters.modified_after == datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


async def test_one_page_costs_at_most_three_statements(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
) -> None:
    """The rows, their ACLs, and the chain — never a query per item.

    Counted on the real connection with a SQLAlchemy event hook, with three
    distinct ACLs planted so a per-item ACL read would show up as extra
    statements rather than as a slower single one.
    """
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree(" ".join(f"f{n:03d}.txt" for n in range(30)), drive=drive)
    acl_ids = [uuid.uuid4() for _ in range(3)]
    for acl_id in acl_ids:
        files_session.add(
            FileAcl(id=acl_id, org_team_id=files_org.org_team_id, body=[], body_hash=acl_id.hex)
        )
    await files_session.flush()
    for index, row in enumerate(made.values()):
        row.acl_id = acl_ids[index % 3]
    await files_session.commit()

    counted: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *_rest: Any) -> None:
        # The transaction's own `SET LOCAL ROLE` / `set_config` are the repo's
        # guard rails, not the listing's work; count reads of Files tables.
        if statement.lstrip().upper().startswith("SELECT") and "FROM file_" in statement:
            counted.append(statement)

    engine = files_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with repo.transaction():
            page = await children(repo, _ctx(files_org), drive.root_node_id, limit=10)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(page.items) == 10
    assert page.next_marker is not None
    assert set(page.acls) <= set(acl_ids) and page.acls
    assert len(counted) <= 3, "\n---\n".join(counted)


async def test_the_page_carries_the_parents_chain_and_a_missing_parent_is_not_found(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree("a/ a/b/ a/b/c.txt", drive=drive)

    async with repo.transaction():
        page = await children(repo, _ctx(files_org), made["a/b"].id, limit=10)
    assert page.chain_ids == [drive.root_node_id, made["a"].id, made["a/b"].id]
    assert [row.id for row in page.items] == [made["a/b/c.txt"].id]
    assert page.has_more is False

    async with repo.transaction():
        with pytest.raises(NotFound):
            await children(repo, _ctx(files_org), uuid.uuid4(), limit=10)


@pytest.mark.parametrize(
    "limit",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(1001, id="over-cap"),
    ],
)
async def test_a_limit_outside_the_published_range_is_refused(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, limit: int
) -> None:
    """Refused, never clamped: a client that asked for 5,000 and silently got
    1,000 would page wrongly and never know."""
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    async with repo.transaction():
        with pytest.raises(InvalidRequest):
            await children(repo, _ctx(files_org), drive.root_node_id, limit=limit)
        # The boundary just inside is accepted.
        page = await children(repo, _ctx(files_org), drive.root_node_id, limit=MAX_LIMIT)
    assert isinstance(page, Page)


async def test_the_readable_predicate_cuts_the_page_after_filtering(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """The authorization hook is a SQL predicate, so an unreadable sibling
    never shortens a page and cannot be inferred from one."""
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree(" ".join(f"f{n:02d}.txt" for n in range(10)), drive=drive)
    hidden = {made[f"f{n:02d}.txt"].id for n in range(0, 10, 2)}

    def readable(node_table: Any) -> Any:
        return node_table.id.not_in(hidden)

    async with repo.transaction():
        page = await children(
            repo,
            _ctx(files_org),
            drive.root_node_id,
            limit=3,
            readable_predicate=readable,
        )
    assert len(page.items) == 3
    assert not {row.id for row in page.items} & hidden


async def test_a_checkpoint_between_the_rows_and_their_acls_can_be_interleaved(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    checkpoints: PausingCheckpoints,
    sessions: Any,
) -> None:
    """The listing's checkpoints are real seams: a writer parked at
    `listing.after_rows` proves the page's rows were read before its ACLs."""
    import asyncio

    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree("a.txt b.txt", drive=drive)
    (writer,) = await sessions(1)
    checkpoints.pause("listing.after_rows")

    async def read() -> Page:
        async with repo.transaction():
            return await children(
                repo, _ctx(files_org), drive.root_node_id, limit=10, checkpoints=checkpoints
            )

    task = asyncio.create_task(read())
    await checkpoints.wait_paused("listing.after_rows")
    # The rows are already read; a delete committed now cannot un-see them.
    await writer.execute(delete(FileNode).where(FileNode.id == made["b.txt"].id))
    await writer.commit()
    checkpoints.release("listing.after_rows")
    page = await task

    assert {row.id for row in page.items} == {made["a.txt"].id, made["b.txt"].id}
    remaining = (
        await writer.execute(select(FileNode.id).where(FileNode.parent_id == drive.root_node_id))
    ).all()
    assert [row[0] for row in remaining] == [made["a.txt"].id]

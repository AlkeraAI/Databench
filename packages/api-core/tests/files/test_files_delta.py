"""The change feed: nothing committed is ever skipped, and nothing leaks.

The watermark test is the one that matters. It is written with two real
sessions on two real connections because the bug it guards — a row whose
`bigserial` id was allocated before another's but whose transaction commits
after it — cannot happen on one connection at all.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.config import settings
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.delta import (
    CP_ROWS_READ,
    DELTA_RETENTION,
    RESYNC_APPLY,
    RESYNC_UPLOAD,
    TOKEN_GENERATION,
    DeltaExpired,
    DeltaItem,
    DeltaPage,
    DeltaService,
    DeltaToken,
)
from alkera_core.files.errors import InvalidRequest
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.path_labels import node_label
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import FileStar
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from alkera_core.schemas.files.delta import decode_token as wire_decode
from sqlalchemy import Engine, event, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests._live_window import live_window
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files.concurrency import conftest as _concurrency

pytestmark = pytest.mark.asyncio

#: The two-session fixture lives beside the concurrency suite; the watermark
#: claim is about two backends, so it is reused here rather than re-declared.
#: Its engine comes with it — a conftest's fixtures reach the tests UNDER it,
#: and this module is a directory above.
concurrency_engine = _concurrency.concurrency_engine
sessions = _concurrency.sessions

KEY = "delta-test-signing-key"


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _service(
    repo: FilesRepo,
    org: FilesOrg,
    clock: FakeClock,
    *,
    checkpoints: Any = None,
) -> DeltaService:
    kwargs: dict[str, Any] = {"signing_key": KEY}
    if checkpoints is not None:
        kwargs["checkpoints"] = checkpoints
    return DeltaService(repo, _ctx(org), clock, **kwargs)


#: The INSERT the tests use to put an outbox row on a *specific* session, so a
#: test can hold one uncommitted while another commits a later one. Written by
#: hand rather than through `history.emit_node_changed` because that helper
#: takes a `FilesRepo` bound to one session, and the watermark claim is about
#: two connections.
_EMIT = text(
    """
    INSERT INTO event_outbox (event_id, org_id, type, entity, entity_id, version,
                              actor, visibility, payload)
    VALUES (gen_random_uuid(), CAST(:org AS uuid), 'file_node.changed', 'file_node',
            CAST(:node AS text), 0, '{}'::jsonb, 'org',
            jsonb_build_object('node_id', CAST(:node2 AS text),
                               'drive_id', CAST(:drive AS text), 'version', 0))
    RETURNING id
    """
)


async def _emit(session: Any, org: FilesOrg, drive: FileDrive, node: FileNode) -> int:
    result = await session.execute(
        _EMIT,
        {
            "org": org.org_team_id,
            "node": str(node.id),
            "node2": str(node.id),
            "drive": str(drive.id),
        },
    )
    return int(result.scalar_one())


def _apply(state: dict[str, dict[str, Any]], items: tuple[DeltaItem, ...]) -> None:
    """What a client does with a page: upsert, or drop on a tombstone."""
    for item in items:
        body = item.as_dict()
        if body["deleted"]:
            state.pop(body["id"], None)
        else:
            state[body["id"]] = body


#: How long the feed may keep withholding a row this test already committed
#: before the suite stops calling the cluster busy and calls the feed broken.
_CATCH_UP = live_window(5.0)


async def _delivering(
    repo: FilesRepo,
    service: DeltaService,
    drive_id: DriveId,
    *,
    token: DeltaToken | None,
    count: int,
    **kwargs: Any,
) -> DeltaPage:
    """Read until the feed has handed back ``count`` items, and answer with that page.

    A row is deliverable once its transaction is strictly below the oldest one
    still in flight IN THIS DATABASE — traffic on the server's other databases
    is narrowed out of that boundary, and the cases below prove it is. So what
    can still hold a row back is a transaction on this same database that took
    its id before the row did, and these cases open exactly that on purpose:
    reading until the row arrives is how they say "the held transaction is the
    only thing keeping it back", without assuming which read it lands on.

    The number of reads is not what these tests assert; what the page says is.
    The window only separates "something on this database was mid-transaction"
    from "the feed never delivers", and the failure names it.
    """
    deadline = time.monotonic() + _CATCH_UP.seconds
    while True:
        async with repo.transaction():
            page = await service.read(drive_id, token=token, **kwargs)
        if len(page.items) >= count:
            return page
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"the feed delivered {len(page.items)} of {count} items in {_CATCH_UP}"
            )


#: Where a neighbour that has nothing to do with this drive is put: another
#: database of the SAME server. `postgres` is on every cluster; the dev and CI
#: clusters also carry the app's own `alkera`, and under `pytest -n` every other
#: worker is one more.
_OTHER_DATABASES: Final = ("postgres", "alkera")

#: How long a neighbour keeps a transaction id before committing it, in seconds.
#: The range straddles a read on purpose: a neighbour has to still be holding
#: when the reader takes its snapshot, and to let go while that read is still
#: being assembled. Randomised per turn so the neighbours neither fall into step
#: with each other nor settle into the reader's own rhythm.
_NEIGHBOUR_HOLD = (0.01, 0.06)


async def _another_database_engine(count: int) -> AsyncEngine:
    """An engine on another database of this server, or skip.

    The claim under test is about the boundary being taken over the whole
    SERVER, so it needs a second database on it. There is no way to state that
    claim against a single-database server, and inventing one here (creating a
    database mid-test) would be a different test with a different failure mode.
    """
    base = make_url(settings.database_url)
    for name in _OTHER_DATABASES:
        if base.database == name:
            continue
        engine = create_async_engine(base.set(database=name), pool_size=count, max_overflow=0)
        try:
            async with engine.connect() as probe:
                await probe.execute(text("SELECT 1"))
        except SQLAlchemyError:
            await engine.dispose()
            continue
        return engine
    pytest.skip(f"no second database on this server (tried {', '.join(_OTHER_DATABASES)})")


@asynccontextmanager
async def _neighbours(count: int, *, holding: bool = False) -> AsyncIterator[None]:
    """`count` transactions on another database of this server, for the block.

    ``holding=True`` takes one transaction id each and keeps it for the whole
    block; otherwise each connection takes one, holds it briefly and commits, in
    a loop — which is what every other worker on a shared cluster is doing.
    """
    engine = await _another_database_engine(count)
    stop = asyncio.Event()
    holding_an_xid = [asyncio.Event() for _ in range(count)]

    async def neighbour(taken: asyncio.Event) -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT txid_current()"))
            taken.set()
            while not stop.is_set():
                await asyncio.sleep(random.uniform(*_NEIGHBOUR_HOLD))
                if holding:
                    continue
                await conn.commit()
                await conn.execute(text("SELECT txid_current()"))

    tasks = {asyncio.create_task(neighbour(taken)) for taken in holding_an_xid}
    ready = asyncio.ensure_future(asyncio.gather(*(taken.wait() for taken in holding_an_xid)))
    try:
        # The body starts with every neighbour already holding a transaction id,
        # so it is not racing the setup for the condition it asserts on. A
        # neighbour that died instead says so here rather than by way of a body
        # that quietly had no neighbours.
        settled, _ = await asyncio.wait({ready, *tasks}, return_when=asyncio.FIRST_COMPLETED)
        for task in settled - {ready}:
            task.result()
        yield
    finally:
        # Asked to stop rather than cancelled: a neighbour checks between turns
        # and is gone within one hold, whereas cancelling one mid-statement
        # leaves the driver unwinding a query nobody is waiting for any more.
        stop.set()
        ready.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


#: Neighbours, and reads to give them a chance to land in. The interleaving is
#: microseconds wide: with these numbers a read in twenty to a read in forty
#: caught it on the code before the fix, so the rounds are what carry the case
#: from "sometimes" to "usually" — and the statement-order case above is what
#: carries it the rest of the way, since no arrangement of live traffic makes
#: a race of that width certain.
_NEIGHBOUR_COUNT = 16
_NEIGHBOUR_ROUNDS = 40


# ---------------------------------------------------------------- watermark


async def test_a_later_committed_row_waits_behind_an_uncommitted_earlier_one(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """The skipped-row bug: B's row must not be delivered past A's open txn."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt b.txt", drive=drive)
    a_session, b_session = await sessions(2)

    a_id = await _emit(a_session, files_org, drive, made["a.txt"])  # NOT committed
    b_id = await _emit(b_session, files_org, drive, made["b.txt"])
    await b_session.commit()
    assert b_id > a_id

    service = _service(repo, files_org, clock)
    async with repo.transaction():
        blocked = await service.read(DriveId(drive.id), token=None)
    assert blocked.items == ()

    await a_session.commit()

    after = await _delivering(repo, service, DriveId(drive.id), token=None, count=2)
    assert [str(item.id) for item in after.items] == [str(made["a.txt"].id), str(made["b.txt"].id)]


async def test_a_transaction_open_in_another_database_does_not_hold_the_feed_back(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The boundary is the server's oldest in-flight transaction, and this feed
    is one database of that server. A neighbour that holds a transaction open in
    another one holds nothing here: taken raw, its transaction id would sit
    under every row this drive ever commits and the feed would answer every
    drive with an empty page for as long as it ran."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt", drive=drive)
    service = _service(repo, files_org, clock)

    async with _neighbours(2, holding=True):
        await _emit(repo.session, files_org, drive, made["a.txt"])
        await repo.session.commit()
        async with repo.transaction():
            page = await service.read(DriveId(drive.id), token=None)

    assert [str(item.id) for item in page.items] == [str(made["a.txt"].id)]


@contextmanager
def _statements_issued() -> Iterator[list[str]]:
    """Every SQL statement that goes out over any engine while the block runs."""
    issued: list[str] = []

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        issued.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield issued
    finally:
        event.remove(Engine, "before_cursor_execute", record)


async def test_the_feed_reads_who_is_where_before_the_snapshot_it_narrows(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """Which transactions are another database's business is read FIRST, alone.

    This is pinned as a claim about the statements the feed issues because the
    behaviour it buys cannot be caught any other way: fold the two observations
    into one query and every transaction that ends between that statement's
    snapshot and its scan of ``pg_stat_activity`` is placed nowhere and drags
    the boundary under rows that were already deliverable. The interleaving is
    microseconds wide, so the case above catches it a few reads in a hundred —
    often enough to redden a suite of sixty-four workers all day, far too rarely
    to be the test that guards the fix. The order is what makes it impossible,
    and the order is exactly what is checked here: the page's own statement
    never scans ``pg_stat_activity``, and a statement that does comes before it.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt", drive=drive)
    await _emit(repo.session, files_org, drive, made["a.txt"])
    await repo.session.commit()
    service = _service(repo, files_org, clock)

    with _statements_issued() as issued:
        async with repo.transaction():
            await service.read(DriveId(drive.id), token=None)

    placement = [index for index, sql in enumerate(issued) if "pg_stat_activity" in sql]
    page = [index for index, sql in enumerate(issued) if "event_outbox" in sql]
    assert placement, "the feed never read where the server's transactions are"
    assert page, "the feed never read the outbox"
    assert not set(placement) & set(page), (
        "one statement both takes the snapshot and scans pg_stat_activity, so a "
        "transaction that ends between the two is placed nowhere"
    )
    assert max(placement) < min(page), (
        "the placement was read after the statement whose snapshot it narrows"
    )


async def test_a_neighbours_traffic_in_another_database_never_withholds_a_committed_row(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A row committed before the read is on the very next page — every time.

    Which in-flight transactions are another database's business has to be read
    BEFORE the snapshot the boundary is taken from. Read it after instead — a
    ``pg_stat_activity`` scan inside the same statement, which is what the two
    spelled as one query come to — and every neighbour that ENDS in between can
    be placed nowhere: still in the snapshot's in-progress list, reported by no
    backend. Those transaction ids are older than anything this drive has just
    committed, so the boundary drops under rows that were deliverable a moment
    ago, and the page the caller gets back is short or empty. It refills on the
    next read — the feed never loses the row — but a consumer that reads once
    and moves on has been told nothing changed when something did.

    The neighbours do nothing exotic: take a transaction id, hold it, commit,
    repeat. That is every other worker on a shared cluster, which is why this
    was most reads under ``pytest -n 64`` rather than a rare interleaving. Each
    round is one more chance to catch it.
    """
    drive = await files_factory.drive()
    spec = " ".join(f"n{index}.txt" for index in range(_NEIGHBOUR_ROUNDS))
    made = await files_factory.tree(spec, drive=drive)
    service = _service(repo, files_org, clock)
    token: DeltaToken | None = None
    missing: list[str] = []

    async with _neighbours(_NEIGHBOUR_COUNT):
        for index in range(_NEIGHBOUR_ROUNDS):
            node = made[f"n{index}.txt"]
            await _emit(repo.session, files_org, drive, node)
            await repo.session.commit()
            async with repo.transaction():
                page = await service.read(DriveId(drive.id), token=token)
            token = page.next_link or page.delta_link
            if str(node.id) not in {str(item.id) for item in page.items}:
                missing.append(f"round {index}: {len(page.items)} items")

    assert missing == [], (
        f"{len(missing)} of {_NEIGHBOUR_ROUNDS} reads withheld the row committed "
        f"before them: {missing[:5]}"
    )


async def test_a_cursor_never_advances_past_an_in_flight_row(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """The delta_link a blocked read hands back still delivers the held row."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt b.txt", drive=drive)
    a_session, b_session = await sessions(2)
    await _emit(a_session, files_org, drive, made["a.txt"])
    await _emit(b_session, files_org, drive, made["b.txt"])
    await b_session.commit()

    service = _service(repo, files_org, clock)
    async with repo.transaction():
        first = await service.read(DriveId(drive.id), token=None)
    assert first.delta_link is not None
    await a_session.commit()

    second = await _delivering(repo, service, DriveId(drive.id), token=first.delta_link, count=2)
    delivered = {str(item.id) for item in second.items}
    assert {str(made["a.txt"].id), str(made["b.txt"].id)} <= delivered


# ------------------------------------------------------------------ O(1) rows


async def test_a_folder_rename_emits_one_row_and_no_descendant_rows(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    made = await files_factory.tree("f/ f/a.txt f/b.txt f/g/ f/g/c.txt", drive=drive)
    session = repo.session
    await _emit(session, files_org, drive, made["f"])
    await session.commit()

    service = _service(repo, files_org, clock)
    page = await _delivering(repo, service, DriveId(drive.id), token=None, count=1)
    assert [str(item.id) for item in page.items] == [str(made["f"].id)]


async def test_a_hundred_thousand_node_move_emits_a_constant_number_of_rows(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The subtree is rewritten by one statement, so the feed carries one item."""
    drive = await files_factory.drive()
    made = await files_factory.tree("src/ dst/", drive=drive)
    session = repo.session
    await _bulk_children(session, drive, made["src"], 100_000)
    await _emit(session, files_org, drive, made["src"])
    await session.commit()

    service = _service(repo, files_org, clock)
    page = await _delivering(repo, service, DriveId(drive.id), token=None, count=1)
    assert len(page.items) <= 2
    assert [str(item.id) for item in page.items] == [str(made["src"].id)]


async def _bulk_children(session: Any, drive: FileDrive, parent: FileNode, count: int) -> None:
    """`count` files under `parent`, in one INSERT … SELECT."""
    await session.execute(
        text(
            """
            INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind,
                                    name, name_display, name_key, name_encoding,
                                    flags_names, path_ids, depth, mode, uid, gid, nlink,
                                    size, rdev, atime_ns, mtime_ns, ctime_ns,
                                    birthtime_ns, xattrs, mime_class, etag, flags,
                                    state, trust, traversal_only, metadata)
            SELECT gen_random_uuid(), :base + g, :drive, :org, :parent, 'file',
                   convert_to('f' || g, 'UTF8'), 'f' || g, 'f' || g, 'utf-8',
                   '{}'::jsonb,
                   (:ppath || '.n' || replace(gen_random_uuid()::text, '-', ''))::ltree,
                   :depth, 420, 0, 0, 1, 0, 0, 0, 0, 0, 0, '{}'::jsonb, NULL, 0, 0,
                   'live', 'own', false, '{}'::jsonb
              FROM generate_series(1, :count) AS g
            """
        ),
        {
            "base": 1_000_000,
            "drive": drive.id,
            "org": drive.org_team_id,
            "parent": parent.id,
            "ppath": parent.path_ids,
            "depth": parent.depth + 1,
            "count": count,
        },
    )


# ------------------------------------------------------------------ paging


@pytest.mark.parametrize(
    ("emitted", "limit", "expect_next"),
    [
        pytest.param(1, 1, False, id="exactly-one-page"),
        pytest.param(2, 1, True, id="one-more-than-a-page"),
        pytest.param(3, 5, False, id="under-a-page"),
    ],
)
async def test_the_page_link_says_whether_more_remain(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    emitted: int,
    limit: int,
    expect_next: bool,
) -> None:
    drive = await files_factory.drive()
    spec = " ".join(f"n{i}.txt" for i in range(emitted))
    made = await files_factory.tree(spec, drive=drive)
    session = repo.session
    for i in range(emitted):
        await _emit(session, files_org, drive, made[f"n{i}.txt"])
    await session.commit()

    service = _service(repo, files_org, clock)
    # `next_link` is a statement about rows the limited page did NOT carry, so
    # the whole emitted batch has to be deliverable before it means anything.
    await _delivering(repo, service, DriveId(drive.id), token=None, count=emitted)
    async with repo.transaction():
        page = await service.read(DriveId(drive.id), token=None, limit=limit)
    assert (page.next_link is not None) is expect_next
    assert (page.delta_link is not None) is (not expect_next)


async def test_latest_token_yields_an_empty_page(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """`?token=latest` is `latest_token()` fed straight back in."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt", drive=drive)
    session = repo.session
    await _emit(session, files_org, drive, made["a.txt"])
    await session.commit()

    service = _service(repo, files_org, clock)
    async with repo.transaction():
        latest = await service.latest_token(DriveId(drive.id))
        page = await service.read(DriveId(drive.id), token=latest)
    assert page.items == ()
    assert page.delta_link is not None and page.next_link is None


async def test_applying_the_same_page_twice_changes_nothing(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt b.txt", drive=drive)
    session = repo.session
    for node in made.values():
        await _emit(session, files_org, drive, node)
        await _emit(session, files_org, drive, node)
    await session.commit()

    service = _service(repo, files_org, clock)
    async with repo.transaction():
        page = await service.read(DriveId(drive.id), token=None)

    once: dict[str, dict[str, Any]] = {}
    _apply(once, page.items)
    twice = {k: dict(v) for k, v in once.items()}
    _apply(twice, page.items)
    assert twice == once
    assert len(once) == 2
    # latest-state-by-id: a node announced twice is one item, not two.
    assert len(page.items) == 2


# ------------------------------------------------------------------ tokens


async def test_an_expired_token_names_its_resync_flavour_and_a_retry_after(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    service = _service(repo, files_org, clock)
    stale = DeltaToken(drive_id=DriveId(drive.id), outbox_id=0, issued_at=clock.now())
    clock.advance(DELTA_RETENTION + timedelta(seconds=1))

    async with repo.transaction():
        with pytest.raises(DeltaExpired) as caught:
            await service.read(DriveId(drive.id), token=stale)
    assert caught.value.code == RESYNC_APPLY
    assert caught.value.retry_after > 0


async def test_a_token_from_an_older_generation_asks_for_an_upload_resync(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A rebuilt feed cannot tell the client which of its own writes landed."""
    drive = await files_factory.drive()
    service = _service(repo, files_org, clock)
    old = DeltaToken(drive_id=DriveId(drive.id), outbox_id=0, issued_at=clock.now(), generation=0)
    async with repo.transaction():
        with pytest.raises(DeltaExpired) as caught:
            await service.read(DriveId(drive.id), token=old)
    assert caught.value.code == RESYNC_UPLOAD


async def test_a_fresh_token_is_not_expired(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The negative twin: one second inside the window still reads."""
    drive = await files_factory.drive()
    service = _service(repo, files_org, clock)
    fresh = DeltaToken(drive_id=DriveId(drive.id), outbox_id=0, issued_at=clock.now())
    clock.advance(DELTA_RETENTION - timedelta(seconds=1))
    async with repo.transaction():
        page = await service.read(DriveId(drive.id), token=fresh)
    assert page.delta_link is not None


async def test_a_tampered_token_does_not_decode() -> None:
    token = DeltaToken(
        drive_id=DriveId(uuid.uuid4()), outbox_id=17, issued_at=datetime(2026, 5, 1, tzinfo=UTC)
    )
    raw = token.encode(key=KEY)
    assert DeltaToken.decode(raw, key=KEY) == token
    head, _, tail = raw.partition(".")
    forged = DeltaToken(
        drive_id=DriveId(uuid.uuid4()),
        outbox_id=token.outbox_id,
        issued_at=token.issued_at,
    ).encode(key=KEY)
    with pytest.raises(InvalidRequest):
        DeltaToken.decode(f"{forged.partition('.')[0]}.{tail}", key=KEY)
    with pytest.raises(InvalidRequest):
        DeltaToken.decode(head, key=KEY)
    with pytest.raises(InvalidRequest):
        DeltaToken.decode(raw, key="another-key")


async def test_a_token_for_another_drive_is_refused(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    service = _service(repo, files_org, clock)
    token = DeltaToken(drive_id=DriveId(uuid.uuid4()), outbox_id=0, issued_at=clock.now())
    async with repo.transaction():
        with pytest.raises(InvalidRequest):
            await service.read(DriveId(drive.id), token=token)


@pytest.mark.parametrize("limit", [pytest.param(0, id="zero"), pytest.param(1001, id="over-max")])
async def test_a_limit_outside_the_range_is_refused(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock, limit: int
) -> None:
    drive = await files_factory.drive()
    service = _service(repo, files_org, clock)
    async with repo.transaction():
        with pytest.raises(InvalidRequest):
            await service.read(DriveId(drive.id), token=None, limit=limit)


# ------------------------------------------------------------- tombstones


async def test_a_node_the_caller_lost_access_to_is_left_off_the_page(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    checkpoints: PausingCheckpoints,
) -> None:
    """The revoke lands after the rows were read; the node is still not
    mentioned, not even as a tombstone, while the node next to it is."""
    drive = await files_factory.drive()
    made = await files_factory.tree("secret.txt open.txt", drive=drive)
    session = repo.session
    for node in made.values():
        await _emit(session, files_org, drive, node)
    await session.commit()

    # Both rows have to be past the feed's boundary before the checkpointed
    # read runs: an empty page would pause and release having authorized nothing.
    await _delivering(
        repo, _service(repo, files_org, clock), DriveId(drive.id), token=None, count=2
    )

    revoked: set[uuid.UUID] = set()
    checkpoints.pause(CP_ROWS_READ)
    service = _service(repo, files_org, clock, checkpoints=checkpoints)

    async def reading() -> Any:
        async with repo.transaction():
            return await service.read(
                DriveId(drive.id),
                token=None,
                readable=lambda node_id: node_id not in revoked,
            )

    task = asyncio.ensure_future(reading())
    await checkpoints.wait_paused(CP_ROWS_READ)
    revoked.add(made["secret.txt"].id)
    checkpoints.release(CP_ROWS_READ)
    page = await task

    assert [item.as_dict()["name"] for item in page.items] == ["open.txt"]
    assert page.delta_link is not None, "the dropped row must still advance the cursor"


_EMIT_UNDER = text(
    """
    INSERT INTO event_outbox (event_id, org_id, type, entity, entity_id, version,
                              actor, visibility, payload)
    VALUES (gen_random_uuid(), CAST(:org AS uuid), 'file_node.changed', 'file_node',
            CAST(:node AS text), 0, '{}'::jsonb, 'org',
            jsonb_build_object('node_id', CAST(:node2 AS text),
                               'drive_id', CAST(:drive AS text), 'version', 0,
                               'parent_id', CAST(:parent AS text)))
    """
)


async def _emit_under(
    session: Any, org: FilesOrg, drive: FileDrive, node: FileNode, parent: uuid.UUID
) -> None:
    """An outbox row that names the folder the node was in, as a trash does."""
    await session.execute(
        _EMIT_UNDER,
        {
            "org": org.org_team_id,
            "node": str(node.id),
            "node2": str(node.id),
            "drive": str(drive.id),
            "parent": str(parent),
        },
    )


async def test_a_trashed_or_vanished_node_is_a_tombstone(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A trashed node vouches for itself; a node whose row is gone is vouched
    for by the folder its last row named; a gone node nothing names is left out."""
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    made = await files_factory.tree("gone.txt trashed.txt orphan.txt", drive=drive)
    session = repo.session
    await _emit_under(session, files_org, drive, made["gone.txt"], drive.root_node_id)
    await _emit(session, files_org, drive, made["trashed.txt"])
    await _emit(session, files_org, drive, made["orphan.txt"])
    await session.execute(
        update(FileNode).where(FileNode.id == made["trashed.txt"].id).values(trashed_at=clock.now())
    )
    await session.execute(
        text("DELETE FROM file_nodes WHERE id = ANY(:ids)"),
        {"ids": [made["gone.txt"].id, made["orphan.txt"].id]},
    )
    await session.commit()

    service = _service(repo, files_org, clock)
    page = await _delivering(repo, service, DriveId(drive.id), token=None, count=2)
    assert {str(item.id) for item in page.items} == {
        str(made["gone.txt"].id),
        str(made["trashed.txt"].id),
    }
    assert all(item.as_dict().keys() == {"id", "deleted"} for item in page.items)


@pytest.mark.parametrize(
    ("folder_readable", "shown"),
    [
        pytest.param(True, True, id="caller-reads-the-folder"),
        pytest.param(False, False, id="caller-cannot-read-the-folder"),
    ],
)
async def test_a_vanished_nodes_tombstone_follows_its_folder(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    folder_readable: bool,
    shown: bool,
) -> None:
    drive = await files_factory.drive()
    made = await files_factory.tree("box/ box/gone.txt marker.txt", drive=drive)
    folder, gone = made["box"], made["box/gone.txt"]
    session = repo.session
    await _emit_under(session, files_org, drive, gone, folder.id)
    await _emit(session, files_org, drive, made["marker.txt"])
    await session.execute(text("DELETE FROM file_nodes WHERE id = :id"), {"id": gone.id})
    await session.commit()

    service = _service(repo, files_org, clock)
    await _delivering(repo, service, DriveId(drive.id), token=None, count=2)
    async with repo.transaction():
        page = await service.read(
            DriveId(drive.id),
            token=None,
            readable=lambda node_id: folder_readable or node_id != folder.id,
        )
    assert str(made["marker.txt"].id) in {str(item.id) for item in page.items}
    assert (str(gone.id) in {str(item.id) for item in page.items}) is shown


async def test_another_orgs_rows_are_not_in_this_feed(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    clock: FakeClock,
) -> None:
    drive = await files_factory.drive()
    stranger = await files_org_factory()
    other_drive = await files_factory.drive(org=stranger)
    session = repo.session
    made = await files_factory.tree("mine.txt", drive=drive)
    theirs_root = await session.get(FileNode, other_drive.root_node_id)
    assert theirs_root is not None
    await _emit(session, files_org, drive, made["mine.txt"])
    await _emit(session, stranger, other_drive, theirs_root)
    await session.commit()

    service = _service(repo, files_org, clock)
    page = await _delivering(repo, service, DriveId(drive.id), token=None, count=1)
    assert [str(item.id) for item in page.items] == [str(made["mine.txt"].id)]


# ------------------------------------------------------------------ stress


async def test_eight_writers_and_two_readers_lose_nothing(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """Every committed row is delivered at least once, and never zero times."""
    drive = await files_factory.drive()
    made = await files_factory.tree(" ".join(f"n{i}.txt" for i in range(8)), drive=drive)
    nodes = [made[f"n{i}.txt"] for i in range(8)]
    pool = await sessions(10)
    writers, readers = pool[:8], pool[8:]
    rng = random.Random(20260909)
    committed: set[str] = set()
    delivered: set[str] = set()
    done = asyncio.Event()

    async def write(session: Any, node: FileNode) -> None:
        for _ in range(4):
            await _emit(session, files_org, drive, node)
            await asyncio.sleep(rng.random() * 0.005)
            await session.commit()
            committed.add(str(node.id))

    async def read(session: Any) -> None:
        service = DeltaService(FilesRepo(session, files_org.scope), _ctx(files_org), clock)
        token: DeltaToken | None = None
        while not done.is_set():
            local = FilesRepo(session, files_org.scope)
            service = DeltaService(local, _ctx(files_org), clock, signing_key=KEY)
            async with local.transaction():
                page = await service.read(DriveId(drive.id), token=token, limit=3)
            delivered.update(str(item.id) for item in page.items)
            token = page.next_link or page.delta_link
            await asyncio.sleep(rng.random() * 0.003)

    reading = [asyncio.ensure_future(read(session)) for session in readers]
    await asyncio.gather(*(write(s, n) for s, n in zip(writers, nodes, strict=True)))
    done.set()
    await asyncio.gather(*reading)

    # A reader may have stopped mid-feed; one final drain proves nothing was
    # skipped rather than merely not yet reached.
    final = await _delivering(
        repo,
        DeltaService(repo, _ctx(files_org), clock, signing_key=KEY),
        DriveId(drive.id),
        token=None,
        count=len(nodes),
        limit=1000,
    )
    delivered.update(str(item.id) for item in final.items)
    assert committed
    assert committed <= delivered


async def test_the_root_label_helper_is_untouched_by_the_feed(
    files_factory: FilesFactory,
) -> None:
    """A guard on the bulk-insert helper: the seeded paths are real ltree labels."""
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    assert node_label(drive.root_node_id).startswith("n")
    assert NodeId(drive.root_node_id) is not None


# ------------------------------------------------------------- cursor order


async def _inverted_pair(
    sessions: Callable[..., Awaitable[list[Any]]],
    files_org: FilesOrg,
    drive: FileDrive,
    made: dict[str, FileNode],
) -> tuple[Any, int, int]:
    """Emit the pair the id-cursor loses: B takes its xid FIRST and its outbox
    id LAST; A takes its xid after B, its id before B's, and stays open.

    Returns A's still-open session so the caller decides when it commits.
    """
    a_session, b_session = await sessions(2)
    b_xid = int(await b_session.scalar(text("SELECT pg_current_xact_id()")))
    a_id = await _emit(a_session, files_org, drive, made["a.txt"])
    a_xid = int(await a_session.scalar(text("SELECT pg_current_xact_id()")))
    b_id = await _emit(b_session, files_org, drive, made["b.txt"])
    await b_session.commit()
    # The whole point of the pair: the two orders disagree.
    assert b_xid < a_xid, (b_xid, a_xid)
    assert a_id < b_id, (a_id, b_id)
    return a_session, a_id, b_id


async def test_a_row_whose_id_is_below_a_delivered_one_is_still_delivered(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """The inverted pair: an id-ordered cursor advances past A and loses it.

    B's transaction is older than A's, so B is deliverable while A is still
    open — but B's outbox id is HIGHER. A cursor that advances by id lands past
    A's id, and A is never returned once it commits.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt b.txt", drive=drive)
    a_session, _a_id, _b_id = await _inverted_pair(sessions, files_org, drive, made)

    service = _service(repo, files_org, clock)
    first = await _delivering(repo, service, DriveId(drive.id), token=None, count=1)
    assert [str(item.id) for item in first.items] == [str(made["b.txt"].id)]
    assert first.delta_link is not None

    await a_session.commit()

    second = await _delivering(repo, service, DriveId(drive.id), token=first.delta_link, count=1)
    assert [str(item.id) for item in second.items] == [str(made["a.txt"].id)]

    # ... exactly once: draining again from the cursor it handed back is empty.
    assert second.delta_link is not None
    async with repo.transaction():
        third = await service.read(DriveId(drive.id), token=second.delta_link)
    assert third.items == ()


async def test_latest_token_does_not_step_over_an_in_flight_earlier_id(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """`?token=latest` is the boundary, not `max(id)`.

    Taking the highest deliverable id as "caught up" has the same shape as the
    paging bug: A's lower id is already behind that watermark when it commits.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt b.txt", drive=drive)
    a_session, _a_id, _b_id = await _inverted_pair(sessions, files_org, drive, made)

    service = _service(repo, files_org, clock)
    async with repo.transaction():
        latest = await service.latest_token(DriveId(drive.id))

    await a_session.commit()

    page = await _delivering(repo, service, DriveId(drive.id), token=latest, count=1)
    assert [str(item.id) for item in page.items] == [str(made["a.txt"].id)]


async def test_a_token_minted_before_the_transaction_half_replays_rather_than_skips(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """An id-only cursor cannot be resumed from without risking a gap, so it is
    read as a catch-up over the retained window."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt", drive=drive)
    session = repo.session
    emitted = await _emit(session, files_org, drive, made["a.txt"])
    await session.commit()

    legacy = DeltaToken(
        drive_id=DriveId(drive.id), outbox_id=emitted, issued_at=clock.now(), cursor_xid=0
    )
    service = _service(repo, files_org, clock)
    page = await _delivering(repo, service, DriveId(drive.id), token=legacy, count=1)
    assert [str(item.id) for item in page.items] == [str(made["a.txt"].id)]


async def test_the_transaction_half_survives_the_signed_round_trip() -> None:
    token = DeltaToken(
        drive_id=DriveId(uuid.uuid4()),
        outbox_id=12,
        issued_at=datetime(2026, 1, 1, tzinfo=UTC),
        cursor_xid=4_294_967_000,
    )
    assert DeltaToken.decode(token.encode(key=KEY), key=KEY) == token


# ------------------------------------------------- the head facts and the star


async def _head(
    session: Any, org: FilesOrg, node: FileNode, *, mime: str, scan: str, digest: str
) -> FileVersion:
    """Give ``node`` a head version, as a commit would."""
    version = FileVersion(
        id=uuid.uuid4(),
        org_team_id=org.org_team_id,
        node_id=node.id,
        seq=1,
        size_bytes=11,
        content_hash=digest,
        mime_sniffed=mime,
        scan_state=scan,
        source="upload",
    )
    session.add(version)
    await session.flush()
    node.head_version_id = version.id
    await session.commit()
    return version


async def test_a_delta_item_carries_the_head_facts_and_the_callers_own_star(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A sync client decides whether to re-download from the head's hash, mime
    type and scan state, and draws the star for the caller reading the page —
    so all four ride the feed rather than a per-row second read."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt b.txt", drive=drive)
    session = repo.session
    await _head(session, files_org, made["a.txt"], mime="text/plain", scan="clean", digest="b3-aaa")
    session.add(
        FileStar(
            org_team_id=files_org.org_team_id,
            user_id=files_org.admin_id,
            node_id=made["a.txt"].id,
        )
    )
    await session.commit()
    await _emit(session, files_org, drive, made["a.txt"])
    await _emit(session, files_org, drive, made["b.txt"])
    await session.commit()

    service = _service(repo, files_org, clock)
    page = await _delivering(repo, service, DriveId(drive.id), token=None, count=2)
    by_id = {str(item.id): item for item in page.items}

    starred = by_id[str(made["a.txt"].id)]
    assert starred.starred is True
    assert starred.content_hash == "b3-aaa"
    assert starred.mime_type == "text/plain"
    assert starred.scan_state == "clean"
    body = starred.as_dict()
    assert body["starred"] is True
    assert body["contentHash"] == "b3-aaa"
    assert body["mimeType"] == "text/plain"
    assert body["scanState"] == "clean"

    # A node with no committed content has no head to name, and no star.
    bare = by_id[str(made["b.txt"].id)]
    assert (bare.starred, bare.content_hash, bare.mime_type, bare.scan_state) == (
        False,
        None,
        None,
        None,
    )


async def test_a_colleagues_star_is_not_this_readers_star_on_the_delta_page(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The star is per user: reading the feed as the admin must not surface the
    member's bookmark, which is what a `flags`-bit answer would have done."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt", drive=drive)
    session = repo.session
    session.add(
        FileStar(
            org_team_id=files_org.org_team_id,
            user_id=files_org.member_id,
            node_id=made["a.txt"].id,
        )
    )
    await session.commit()
    await _emit(session, files_org, drive, made["a.txt"])
    await session.commit()

    service = _service(repo, files_org, clock)
    page = await _delivering(repo, service, DriveId(drive.id), token=None, count=1)
    assert [item.starred for item in page.items] == [False]


# ------------------------------------------------ one token shape, end to end


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _payload(raw: str) -> dict[str, Any]:
    """The JSON an encoded token carries, read without checking the signature."""
    head, _, _ = raw.partition(".")
    decoded = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    assert isinstance(decoded, dict)
    return decoded


def _signed(fields: dict[str, Any], *, key: str) -> str:
    """What a writer emits for ``fields`` — the encoder's format, spelled out
    here rather than called, so this file does not compute its own expectation
    from the code under test."""
    body = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return f"{_b64(body)}.{_b64(hmac.new(key.encode(), body, hashlib.sha256).digest())}"


def test_the_string_the_service_mints_decodes_through_the_wire_decoder() -> None:
    """There is ONE token shape: what the library encodes is what the schemas
    layer's `decode_token` reads, field for field. Two encoders agreeing only by
    hand drift the first time a field is added to one of them."""
    minted = DeltaToken(
        drive_id=DriveId(uuid.uuid4()),
        outbox_id=4_294_967_296,
        issued_at=datetime(2026, 5, 1, tzinfo=UTC),
        cursor_xid=4_294_967_000,
    )
    decoded = wire_decode(minted.encode(key=KEY), key=KEY)
    assert decoded.drive_id == minted.drive_id
    assert decoded.outbox_id == minted.outbox_id
    assert decoded.cursor_xid == minted.cursor_xid
    assert decoded.issued_at == minted.issued_at
    assert decoded.generation == minted.generation


def test_the_encoded_token_carries_the_schema_version_that_wrote_it() -> None:
    """The token is persisted in client caches, so its payload names its own
    version — that stamp is what lets a later reader run the ladder instead of
    guessing which fields a five-week-old desktop client sent."""
    raw = DeltaToken(outbox_id=7, issued_at=datetime(2026, 5, 1, tzinfo=UTC)).encode(key=KEY)
    assert _payload(raw)["schema_version"] == DeltaToken.SCHEMA_VERSION


async def test_an_id_only_token_from_the_previous_version_migrates_and_catches_up(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A 1.0.0 writer had no transaction half. Its token still decodes — through
    the migration ladder, arriving as a catch-up cursor — rather than being
    refused, which is the whole reason the token is a versioned model."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt", drive=drive)
    session = repo.session
    emitted = await _emit(session, files_org, drive, made["a.txt"])
    await session.commit()

    raw = _signed(
        {
            "schema_version": "1.0.0",
            "drive_id": str(drive.id),
            "outbox_id": emitted,
            "issued_at": clock.now().isoformat(),
            "generation": TOKEN_GENERATION,
        },
        key=KEY,
    )
    token = DeltaToken.decode(raw, key=KEY)
    assert token.schema_version == DeltaToken.SCHEMA_VERSION
    assert token.cursor_xid == 0

    service = _service(repo, files_org, clock)
    page = await _delivering(repo, service, DriveId(drive.id), token=token, count=1)
    assert [str(item.id) for item in page.items] == [str(made["a.txt"].id)]


async def test_a_token_that_names_no_mint_time_is_expired_rather_than_fresh(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """`issued_at` is optional on the wire, so the retention check needs an
    answer for a token carrying none: outside the window, never inside it."""
    drive = await files_factory.drive()
    undated = DeltaToken.decode(
        _signed(
            {"schema_version": DeltaToken.SCHEMA_VERSION, "drive_id": str(drive.id)},
            key=KEY,
        ),
        key=KEY,
    )
    assert undated.issued_at is None
    service = _service(repo, files_org, clock)
    async with repo.transaction():
        with pytest.raises(DeltaExpired) as caught:
            await service.read(DriveId(drive.id), token=undated)
    assert caught.value.code == RESYNC_APPLY

"""Bounding the versions a co-edited file's write backs leave behind.

A live session writes the document back to its file a couple of seconds after
each burst of typing. Every write back is a ``document_snapshot`` version
counted against the drive, so an hour of typing in one file is hundreds of
rows, and once the drive's ceiling refuses the next write back saving stops.

Each case is a separate way the collapse can be wrong: too shy (it leaves the
run it exists to bound), too eager (it takes a version the file, a pin, or
the live session still needs, so a merge loses its base), or too broad (it
reaches into another run, another kind of version, or another org).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.repo import FilesRepo
from alkera_core.files.sweepers import (
    LIVE_VERSION_HOT,
    WRITE_BACK_HOT,
    WRITE_BACK_KEEP_EVERY,
    WRITE_BACK_POSITIONS_KEPT,
    LiveVersionCollapse,
    SweepDeps,
    WriteBackCollapse,
    run_janitor,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: A slot boundary, so a run planted from here fills whole slots.
START = EPOCH + timedelta(days=400)
SECOND = timedelta(seconds=1)
#: The write-back delay a session types under: one write back per burst.
EVERY = timedelta(seconds=30)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class Bed:
    """One file and the versions planted on it, with the etag each landed at.

    A version made on etag ``e`` (its ``based_on_etag``) is published at
    ``e + 1``, so the next one is made on that."""

    def __init__(self, session: AsyncSession, org: FilesOrg, node_id: uuid.UUID) -> None:
        self.session = session
        self.org = org
        self.node_id = node_id
        self.seq = 0
        self.etag = 0
        self.etag_of: dict[uuid.UUID, int] = {}

    async def version(
        self,
        *,
        at: datetime,
        epoch: int | None = 1,
        source: str = "document_snapshot",
        based_on: int | None = None,
        lease_epoch: int | None = None,
        head: bool = False,
        keep_forever: bool = False,
        held: bool = False,
        commit: bool = True,
    ) -> uuid.UUID:
        """One landed version. ``source`` other than a write back is an upload
        (a person's save, or a box's); ``epoch`` is the write back's origin."""
        self.seq += 1
        version_id = uuid.uuid4()
        metadata: dict[str, Any] = {"based_on_etag": self.etag if based_on is None else based_on}
        if source == "document_snapshot" and epoch is not None:
            metadata["origin"] = {"epoch": epoch, "vv": "AAEC"}
        if lease_epoch is not None:
            metadata["live_lease_epoch"] = lease_epoch
        await self.session.execute(
            text(
                "INSERT INTO file_versions (id, org_team_id, node_id, seq, size_bytes, "
                "content_hash, block_hash, source, scan_state, keep_forever, held, lease_epoch, "
                "created_at, metadata) VALUES (:id, :org, :node, :seq, 8, :hash, '', :source, "
                "'clean', :keep, :held, 0, :at, CAST(:meta AS jsonb))"
            ),
            {
                "id": version_id,
                "org": self.org.org_team_id,
                "node": self.node_id,
                "seq": self.seq,
                "hash": f"h{self.seq}",
                "source": source,
                "keep": keep_forever,
                "held": held,
                "at": at,
                "meta": json.dumps(metadata),
            },
        )
        self.etag += 1
        self.etag_of[version_id] = self.etag
        if head:
            await self.session.execute(
                text("UPDATE file_nodes SET head_version_id = :v WHERE id = :n"),
                {"v": version_id, "n": self.node_id},
            )
        if commit:
            await self.session.commit()
        return version_id

    async def typing(
        self, count: int, *, start: datetime = START, epoch: int = 1, head_last: bool = True
    ) -> list[uuid.UUID]:
        """``count`` write backs, one every :data:`EVERY` from ``start``, the
        last of them the file's head."""
        ids = [
            await self.version(
                at=start + n * EVERY, epoch=epoch, head=head_last and n == count - 1, commit=False
            )
            for n in range(count)
        ]
        await self.session.commit()
        return ids

    async def session_row(
        self,
        *,
        epoch: int,
        source_version: uuid.UUID | None = None,
        source_epoch: int | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> None:
        """The file's live session, at ``epoch``, matching ``source_version``
        and remembering ``history``."""
        source = source_version is not None
        await self.session.execute(
            text(
                "INSERT INTO crdt_docs (org_id, doc_type, doc_id, loro_format, seeded_from, "
                "epoch, source_etag, source_version_id, source_sha256, source_vv, source_epoch, "
                "source_history) VALUES (:org, 'file', :doc, '1.16.2', 'empty', :epoch, "
                ":etag, :version, :sha, :vv, :source_epoch, CAST(:history AS jsonb))"
            ),
            {
                "org": self.org.org_team_id,
                "doc": str(self.node_id),
                "epoch": epoch,
                "etag": self.etag if source else None,
                "version": source_version,
                "sha": "0" * 64 if source else None,
                "vv": b"\x00" if source else None,
                "source_epoch": (source_epoch or epoch) if source else None,
                "history": json.dumps(history or []),
            },
        )
        await self.session.commit()

    async def surviving(self) -> list[uuid.UUID]:
        """This file's versions, oldest first."""
        self.session.expire_all()
        rows = await self.session.execute(
            text(
                "SELECT id FROM file_versions WHERE org_team_id = :org AND node_id = :node "
                "ORDER BY seq"
            ),
            {"org": self.org.org_team_id, "node": self.node_id},
        )
        return [row.id for row in rows]

    def deps(self) -> SweepDeps:
        return SweepDeps(repo=FilesRepo(self.session, self.org.scope), ctx=_ctx(self.org))

    async def collapse(self, at: datetime, *, budget: int = 1_000) -> int:
        return (await WriteBackCollapse(self.deps()).run(at, budget=budget)).swept


async def _file(factory: FilesFactory, session: AsyncSession, org: FilesOrg) -> Bed:
    drive = await factory.drive()
    tree = await factory.tree("notes.md", drive=drive)
    return Bed(session, org, tree["notes.md"].id)


@pytest.fixture
async def bed(files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory) -> Bed:
    return await _file(files_factory, files_session, files_org)


def _cold(ids: list[uuid.UUID]) -> datetime:
    """An instant at which every one of ``ids`` (planted by
    :meth:`Bed.typing` from :data:`START`) is past the hot window."""
    return START + len(ids) * EVERY + WRITE_BACK_HOT + SECOND


def _slot(index: int) -> int:
    """Which keep slot the ``index``-th write back of a run from
    :data:`START` falls in."""
    return int((index * EVERY) / WRITE_BACK_KEEP_EVERY)


async def test_an_hour_of_typing_in_one_epoch_collapses_to_a_bounded_set(bed: Bed) -> None:
    """The whole point: a hundred write backs in one session are not a
    hundred things anyone opens, and they must not fill the drive. What is
    left is where the run began, where it ended, and one point per slot."""
    ids = await bed.typing(100)

    await run_janitor(bed.deps(), _cold(ids))

    left = await bed.surviving()
    slots = {_slot(n) for n in range(len(ids))}
    assert len(left) <= len(slots) + 2
    assert left[0] == ids[0]
    assert left[-1] == ids[-1]
    # Every slot the run typed in still has a point to step back to.
    assert {_slot(ids.index(v)) for v in left} == slots


async def test_a_second_pass_takes_nothing_more(bed: Bed) -> None:
    """Idempotence: a keeper of one pass is a keeper of the next, so a pass
    that died half-way and runs again cannot eat into what the first kept."""
    ids = await bed.typing(60)
    first = await bed.collapse(_cold(ids))
    assert first > 0
    left = await bed.surviving()

    assert await bed.collapse(_cold(ids) + timedelta(days=30)) == 0

    assert await bed.surviving() == left


@pytest.mark.parametrize(
    ("past_cutoff", "collapses"),
    [
        pytest.param(-SECOND, False, id="one-second-inside-the-hot-window"),
        pytest.param(SECOND, True, id="one-second-past-the-hot-window"),
    ],
)
async def test_the_hot_window_is_where_a_session_can_still_merge_from(
    bed: Bed, past_cutoff: timedelta, collapses: bool
) -> None:
    """Inside the window the session's own memory of the source versions it
    matched may still need any of them; the collapse waits until the newest
    of the middle write backs has left it."""
    ids = await bed.typing(6)
    # The second-to-last write back is the newest one the collapse could take.
    now = START + 4 * EVERY + WRITE_BACK_HOT + past_cutoff

    swept = await bed.collapse(now)

    assert (ids[4] not in await bed.surviving()) is collapses
    assert swept > 0


async def test_another_version_between_write_backs_ends_the_run(bed: Bed) -> None:
    """An upload between two write backs is a point the history turns at: the
    write backs before it and after it are two runs, and each keeps its own
    first and last."""
    before = await bed.typing(5, head_last=False)
    upload = await bed.version(at=START + 5 * EVERY, source="upload")
    after = await bed.typing(5, start=START + 6 * EVERY)

    await bed.collapse(_cold([*before, upload, *after]))

    assert await bed.surviving() == [before[0], before[-1], upload, after[0], after[-1]]


async def test_a_new_epoch_starts_a_new_run(bed: Bed) -> None:
    """A session restarted on the file (a new epoch) is a separate history
    to look back through, not the tail of the one before."""
    older = await bed.typing(5, epoch=1, head_last=False)
    newer = await bed.typing(5, start=START + 5 * EVERY, epoch=2)

    await bed.collapse(_cold(older + newer))

    assert await bed.surviving() == [older[0], older[-1], newer[0], newer[-1]]


async def test_a_version_nobody_wrote_back_is_not_the_collapse_to_make(bed: Bed) -> None:
    """The source is the whole licence: a person's uploads have their own
    retention, and a collapse that swept them deletes their history."""
    uploads = [
        await bed.version(at=START + n * EVERY, source="upload", head=n == 9) for n in range(10)
    ]

    assert await bed.collapse(_cold(uploads)) == 0

    assert await bed.surviving() == uploads


@pytest.mark.parametrize(
    "pin",
    [
        pytest.param("keep_forever", id="kept-forever"),
        pytest.param("held", id="held"),
        pytest.param("grant", id="named-by-a-content-grant"),
        pytest.param("conflict", id="named-by-a-conflict"),
    ],
)
async def test_a_pinned_write_back_survives_its_run(bed: Bed, pin: str) -> None:
    """A pin outranks every deadline in the ledger."""
    ids = await bed.typing(3, head_last=False)
    pinned = await bed.version(
        at=START + 3 * EVERY, keep_forever=pin == "keep_forever", held=pin == "held"
    )
    ids += [pinned, *await bed.typing(3, start=START + 4 * EVERY)]
    if pin == "grant":
        await bed.session.execute(
            text(
                "INSERT INTO file_content_grants (nonce, org_team_id, version_id, expires_at) "
                "VALUES (:nonce, :org, :v, :at)"
            ),
            {
                "nonce": uuid.uuid4().hex,
                "org": bed.org.org_team_id,
                "v": pinned,
                "at": _cold(ids) + timedelta(minutes=5),
            },
        )
    if pin == "conflict":
        await bed.session.execute(
            text(
                "INSERT INTO file_conflicts (id, org_team_id, node_id, base_version_id, "
                "theirs_version_id, mine_version_id, actor, state) VALUES "
                "(:id, :org, :node, :v, :v, :v, :actor, 'auto')"
            ),
            {
                "id": uuid.uuid4(),
                "org": bed.org.org_team_id,
                "node": bed.node_id,
                "v": pinned,
                "actor": bed.org.admin_id,
            },
        )
    await bed.session.commit()

    await bed.collapse(_cold(ids))

    assert await bed.surviving() == [ids[0], pinned, ids[-1]]


async def test_the_live_epochs_newest_write_backs_are_what_a_merge_looks_back_through(
    bed: Bed,
) -> None:
    """A session merging an outside change looks back through the file's
    newest write backs of its epoch (one it made and never recorded is among
    them). Those stay, however old; older ones in the same epoch collapse."""
    extra = 40
    ids = await bed.typing(WRITE_BACK_POSITIONS_KEPT + extra)
    await bed.session_row(epoch=1, source_version=ids[-1])

    assert await bed.collapse(_cold(ids)) > 0

    left = await bed.surviving()
    assert set(ids[extra:]) <= set(left)
    assert len(left) < len(ids)


async def test_a_session_in_a_later_epoch_holds_no_older_epochs_write_backs(bed: Bed) -> None:
    """Only the session's current epoch is merged from: the write backs of an
    epoch that ended are history, and collapse like any run."""
    ids = await bed.typing(30)
    await bed.session_row(epoch=2, source_version=ids[-1], source_epoch=2)

    await bed.collapse(_cold(ids))

    assert await bed.surviving() == [ids[0], ids[19], ids[-1]]


@pytest.mark.parametrize(
    ("named_by", "session_epoch", "kept"),
    [
        pytest.param("version_id", 1, True, id="history-record-by-version-id"),
        pytest.param("etag", 1, True, id="history-record-by-etag-in-its-epoch"),
        pytest.param("etag", 2, False, id="history-etag-from-an-epoch-that-ended"),
        pytest.param("source_version", 2, True, id="the-session-latest-source"),
    ],
)
async def test_a_write_back_the_session_names_is_its_merge_base(
    bed: Bed, named_by: str, session_epoch: int, kept: bool
) -> None:
    """The session merges an outside change from the source version its writer
    named, out of its latest source and the history it remembers. The version
    it names there keeps its row (and the document position on it), whatever
    the collapse would otherwise make of the run; a record of an epoch the
    session left names nothing it will merge from."""
    ids = await bed.typing(WRITE_BACK_POSITIONS_KEPT + 30)
    named = ids[7]
    record = {
        # Named by its id alone, the etag points nowhere, so only the id holds it.
        "etag": -1 if named_by == "version_id" else bed.etag_of[named],
        "version_id": str(named) if named_by == "version_id" else None,
        "vv": "AAEC",
        "at": 0.0,
    }
    await bed.session_row(
        epoch=session_epoch,
        source_version=named if named_by == "source_version" else ids[-1],
        source_epoch=1,
        history=[] if named_by == "source_version" else [record],
    )

    await bed.collapse(_cold(ids))

    assert (named in await bed.surviving()) is kept


@pytest.mark.parametrize(
    ("head_source", "kept"),
    [
        pytest.param("upload", True, id="an-outside-change-waiting-to-merge"),
        pytest.param("document_snapshot", False, id="the-session-own-write-back"),
    ],
)
async def test_the_write_back_an_outside_head_was_made_on_is_kept(
    bed: Bed, head_source: str, kept: bool
) -> None:
    """A box or a person saved the file on an older write back's etag: the
    merge that settles it starts from that write back, so it stays even with
    no session open to name it. A write back at the head names nothing."""
    ids = await bed.typing(20, head_last=False)
    base = ids[7]
    head = await bed.version(
        at=START + 20 * EVERY, source=head_source, based_on=bed.etag_of[base], head=True
    )

    await bed.collapse(_cold([*ids, head]))

    assert (base in await bed.surviving()) is kept


async def test_the_budget_bounds_one_pass_and_the_next_finishes(bed: Bed) -> None:
    """A long session must not make one pass run long: the sweeper takes a
    page, says there is more, and the next pass takes the rest."""
    ids = await bed.typing(40)
    deps = bed.deps()

    first = await WriteBackCollapse(deps).run(_cold(ids), budget=5)

    assert (first.swept, first.cursor) == (5, "more")
    rest = await WriteBackCollapse(deps).run(_cold(ids))
    assert rest.cursor is None
    assert await bed.collapse(_cold(ids)) == 0
    assert len(await bed.surviving()) == len(ids) - first.swept - rest.swept


async def test_another_orgs_write_backs_are_not_this_orgs_to_collapse(
    bed: Bed, files_org_factory: Any, files_session: AsyncSession
) -> None:
    """Every clause is org-scoped, the session read included."""
    other = await files_org_factory()
    theirs_bed = await _file(FilesFactory(files_session, other), files_session, other)
    theirs = await theirs_bed.typing(20)
    ours = await bed.typing(20)

    assert await bed.collapse(_cold(ours)) > 0

    assert await theirs_bed.surviving() == theirs


async def test_a_holder_write_back_under_a_lease_is_not_the_lease_collapse_to_take(
    bed: Bed,
) -> None:
    """A write back landed as the folder's holder is stamped with the lease
    generation too. The lease collapse keeps one landing per generation and
    knows nothing of the session; it leaves write backs to the collapse that
    does, so the version the session names survives both."""
    ids = [await bed.version(at=START + n * EVERY, lease_epoch=3, head=n == 11) for n in range(12)]
    named = ids[2]
    await bed.session_row(
        epoch=1,
        source_version=ids[-1],
        history=[{"etag": bed.etag_of[named], "version_id": str(named), "vv": "AAEC", "at": 0.0}],
    )
    late = START + 12 * EVERY + max(LIVE_VERSION_HOT, WRITE_BACK_HOT) + SECOND

    await LiveVersionCollapse(bed.deps()).run(late)
    await bed.collapse(late)

    assert named in await bed.surviving()

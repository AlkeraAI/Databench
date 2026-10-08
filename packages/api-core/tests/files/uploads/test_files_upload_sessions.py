"""Upload sessions: quota at open, the TTL bump, resume, and the part races.

Everything runs against real Postgres on this run's lane database and a real
``FilesystemStore`` wrapped in ``FaultyStore``, so "the bytes are there" is read
back off the disk and "no second store call" is read off the driver's own call
log rather than a mock's. The crash in the resume test is a killed checkpoint —
the coroutine dies between the store write and the part row exactly where a
SIGKILL would — and the two-backend races are forced with ``sessions(2)`` and a
paused checkpoint, never with a sleep.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from alkera_core.authz.principal import ActingContext, Principal, PrincipalKind
from alkera_core.config import settings
from alkera_core.files.checkpoints import CheckpointKilled, PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.errors import Conflict, InvalidRequest, NotFound, TooLarge
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.ids import DomainId, DriveId, NodeId, OrgScope, SessionId
from alkera_core.files.names import NAME_MAX_BYTES
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import DomainStore, _RootedDomainStore
from alkera_core.files.uploads import UploadService, part_key, parts_total
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.uploads import FileUploadPart, FileUploadSession
from alkera_test_support.files.faulty_store import FaultSchedule, FaultyStore
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

#: The tests upload a handful of tiny parts, so the service is built with a
#: part size they can actually cross rather than 32 MiB of zeros.
SMALL_PART = 8


def _ctx() -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER, id=str(uuid.uuid4()), org_id=uuid.uuid4()
        )
    )


def _digest(payload: bytes) -> bytes:
    """The BLAKE3 the store verifies the stream against."""
    return hash_bytes(payload).content_hash


async def _stream(payload: bytes) -> AsyncIterator[bytes]:
    """The part as a client sends it: several chunks, not one buffer."""
    for start in range(0, len(payload), 7):
        yield payload[start : start + 7]


@dataclass(frozen=True, slots=True)
class Rig:
    """A drive, a folder to upload into, and the service under test."""

    service: UploadService
    repo: FilesRepo
    store: FaultyStore
    root: Path
    drive_uuid: uuid.UUID
    folder_uuid: uuid.UUID
    org: FilesOrg
    domain_id: DomainId
    checkpoints: PausingCheckpoints
    store_handle: object

    @property
    def drive_id(self) -> DriveId:
        return DriveId(self.drive_uuid)

    @property
    def folder_id(self) -> NodeId:
        return NodeId(self.folder_uuid)

    def object_path(self, session_id: SessionId, part_no: int, payload: bytes) -> Path:
        key = part_key(session_id, part_no, _digest(payload))
        return self.root / "domains" / str(self.domain_id) / key


@pytest.fixture
async def rig(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    checkpoints: PausingCheckpoints,
    tmp_path: Path,
) -> Rig:
    drive = await files_factory.drive()
    tree = await files_factory.tree("uploads/", drive=drive)
    domain_id = DomainId(drive.dedup_domain_id)
    # The production filesystem handle is rooted at the domain's own directory
    # (``ScopedStoreFactory``), so the driver only ever sees relative keys.
    domain_root = tmp_path / "domains" / str(domain_id)
    inner = FaultyStore(FilesystemStore(domain_root, clock=clock.now), FaultSchedule(faults=()))
    store = _RootedDomainStore(inner, domain_id)
    repo = FilesRepo(files_session, OrgScope(org_team_id=files_org.org_team_id))
    return Rig(
        service=UploadService(
            repo, _ctx(), clock, store, part_size=SMALL_PART, checkpoints=checkpoints
        ),
        repo=repo,
        store=inner,
        root=tmp_path,
        drive_uuid=drive.id,
        folder_uuid=tree["uploads"].id,
        org=files_org,
        domain_id=domain_id,
        checkpoints=checkpoints,
        store_handle=store,
    )


async def _second_backend(rig: Rig, engine: AsyncEngine) -> tuple[UploadService, AsyncSession]:
    """A second backend on its own connection, sharing the store and the tenant.

    The connection comes from the suite's pool: both backends hold a transaction
    open across the checkpoint their race is decided at, so neither can be
    handed the other's.
    """
    session = AsyncSession(bind=engine)
    service = UploadService(
        FilesRepo(session, rig.repo.scope),
        _ctx(),
        FakeClock(now=EPOCH),
        cast(DomainStore, rig.store_handle),
        part_size=SMALL_PART,
        checkpoints=rig.checkpoints,
    )
    return service, session


async def _shrink_quota(session: AsyncSession, drive_id: uuid.UUID, *, quota_bytes: int) -> None:
    await session.execute(
        update(FileDrive.__table__).where(FileDrive.id == drive_id).values(quota_bytes=quota_bytes)
    )
    await session.commit()


async def _held(session: AsyncSession, drive_id: uuid.UUID) -> int:
    held = await session.execute(
        select(func.coalesce(func.sum(FileUploadSession.quota_hold_bytes), 0)).where(
            FileUploadSession.drive_id == drive_id,
            FileUploadSession.state.in_(("open", "uploading", "committing")),
        )
    )
    return int(held.scalar_one())


# ---- open ---------------------------------------------------------------


async def test_open_reserves_quota_and_cuts_the_declared_size_into_parts(rig: Rig) -> None:
    session = await rig.service.open(
        rig.drive_id, rig.folder_id, b"big.bin", declared_size=SMALL_PART * 3 + 1
    )
    assert session.state == "open"
    assert session.part_size == SMALL_PART
    assert session.parts_total == 4
    assert session.expires_at > EPOCH
    assert await _held(rig.repo.session, rig.drive_uuid) == SMALL_PART * 3 + 1


async def test_open_over_quota_refuses_before_any_byte_reaches_the_store(rig: Rig) -> None:
    """The refusal is a quota decision, so the driver never sees a call."""
    from alkera_core.files.errors import QuotaExceeded

    await _shrink_quota(rig.repo.session, rig.drive_uuid, quota_bytes=10)
    with pytest.raises(QuotaExceeded) as raised:
        await rig.service.open(rig.drive_id, rig.folder_id, b"big.bin", declared_size=11)
    assert raised.value.kind == "bytes"
    assert rig.store.calls == []
    assert await _held(rig.repo.session, rig.drive_uuid) == 0


@pytest.mark.parametrize(
    ("name", "code"),
    [
        pytest.param(b"", "files.invalid_name.empty", id="empty"),
        pytest.param(b"a/b", "files.invalid_name.separator", id="separator"),
        pytest.param(b"..", "files.invalid_name.dot", id="dotdot"),
        pytest.param(b"a\x00b", "files.invalid_name.nul", id="nul"),
        pytest.param(b"a" * (NAME_MAX_BYTES + 1), "files.invalid_name.too_long", id="too-long"),
        pytest.param(b"a\tb", "files.invalid_name.control", id="control"),
        pytest.param(b" notes", "files.invalid_name.surrounding_space", id="leading-space"),
    ],
)
async def test_open_refuses_a_name_no_machine_could_hold(rig: Rig, name: bytes, code: str) -> None:
    with pytest.raises(InvalidRequest) as raised:
        await rig.service.open(rig.drive_id, rig.folder_id, name, declared_size=1)
    assert raised.value.code == code


async def test_open_refuses_a_size_over_the_deployment_s_file_cap(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling is decided at open, before quota is touched or a byte moves.

    The refusal is the sentence a reader is shown, so it names the limit in the
    units the product states it in and nothing else: no byte counts, no setting
    name, no code. A reader who meets it has to decide what to send instead,
    and "files.too_large: declared size 101 bytes" is not an answer to that.
    """
    monkeypatch.setattr(settings, "files_max_file_bytes", 2_000_000_000_000)
    with pytest.raises(TooLarge) as raised:
        await rig.service.open(
            rig.drive_id, rig.folder_id, b"big.bin", declared_size=2_000_000_000_001
        )
    assert raised.value.code == "files.too_large"
    assert raised.value.status == 413
    assert str(raised.value) == "exceeded the maximum upload size of 2000 GB"
    monkeypatch.setattr(settings, "files_max_file_bytes", 100)
    with pytest.raises(TooLarge) as raised:
        await rig.service.open(rig.drive_id, rig.folder_id, b"big.bin", declared_size=101)
    assert str(raised.value) == "exceeded the maximum upload size of 100 B"
    assert rig.store.calls == []
    assert await _held(rig.repo.session, rig.drive_uuid) == 0
    admitted = await rig.service.open(rig.drive_id, rig.folder_id, b"big.bin", declared_size=100)
    assert admitted.state == "open"


async def test_open_refuses_a_size_needing_more_parts_than_the_deployment_allows(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session nobody could finish is refused, not opened and then stranded.

    Without this the part cap bites one part at a time: the client fills the
    session until a part number lands outside the range, having spent the whole
    transfer to learn a number the server knew at open.
    """
    monkeypatch.setattr(settings, "files_max_upload_parts", 2)
    with pytest.raises(TooLarge) as raised:
        await rig.service.open(
            rig.drive_id, rig.folder_id, b"big.bin", declared_size=SMALL_PART * 3
        )
    assert raised.value.code == "files.too_large"
    assert "3 parts" in str(raised.value) and "limit of 2" in str(raised.value)
    assert await _held(rig.repo.session, rig.drive_uuid) == 0
    admitted = await rig.service.open(
        rig.drive_id, rig.folder_id, b"big.bin", declared_size=SMALL_PART * 2
    )
    assert admitted.parts_total == 2


async def test_open_refuses_once_the_org_holds_the_maximum_live_sessions(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling is behavioural: two open, the third is refused, and after an
    abort takes one out of the live set a new one is admitted again."""
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "files_max_open_sessions_per_org", 2)
    first = await rig.service.open(rig.drive_id, rig.folder_id, b"a.bin", declared_size=1)
    await rig.service.open(rig.drive_id, rig.folder_id, b"b.bin", declared_size=1)
    with pytest.raises(Conflict) as raised:
        await rig.service.open(rig.drive_id, rig.folder_id, b"c.bin", declared_size=1)
    assert raised.value.code == "files.too_many_sessions"
    await rig.service.abort(first.id)
    admitted = await rig.service.open(rig.drive_id, rig.folder_id, b"c.bin", declared_size=1)
    assert admitted.state == "open"


# ---- parts --------------------------------------------------------------


async def test_an_accepted_part_lands_in_the_store_and_moves_the_offset(rig: Rig) -> None:
    session = await rig.service.open(
        rig.drive_id, rig.folder_id, b"one.bin", declared_size=len(b"hello wo")
    )
    payload = b"hello wo"
    result = await rig.service.put_part(
        session.id, 1, _stream(payload), size=len(payload), checksum=_digest(payload)
    )
    assert result.duplicate is False
    assert rig.object_path(session.id, 1, payload).read_bytes() == payload
    status = await rig.service.status(session.id)
    assert status.state == "uploading"
    assert status.accepted_parts == (1,)
    assert status.offset == len(payload)
    assert status.complete is True


async def test_the_ttl_is_pushed_out_by_every_accepted_part(rig: Rig) -> None:
    """The deadline comes from Postgres ``now()``, so it moves with the second
    part even though the injected clock is what the test advances."""
    session = await rig.service.open(rig.drive_id, rig.folder_id, b"two.bin", declared_size=16)
    before = (await rig.service.status(session.id)).expires_at
    await rig.service.put_part(session.id, 1, _stream(b"1234"), size=4, checksum=_digest(b"1234"))
    first = (await rig.service.status(session.id)).expires_at
    await asyncio.sleep(0)
    await rig.service.put_part(session.id, 2, _stream(b"5678"), size=4, checksum=_digest(b"5678"))
    second = (await rig.service.status(session.id)).expires_at
    assert first > before
    assert second >= first


@pytest.mark.parametrize(
    "part_no",
    [pytest.param(0, id="below-first"), pytest.param(3, id="past-last")],
)
async def test_a_part_number_outside_the_declared_range_is_refused(rig: Rig, part_no: int) -> None:
    session = await rig.service.open(
        rig.drive_id, rig.folder_id, b"r.bin", declared_size=SMALL_PART + 1
    )
    assert parts_total(SMALL_PART + 1, SMALL_PART) == 2
    with pytest.raises(InvalidRequest) as raised:
        await rig.service.put_part(
            session.id, part_no, _stream(b"x"), size=1, checksum=_digest(b"x")
        )
    assert raised.value.code == "files.part_out_of_range"
    assert rig.store.calls == []


async def test_a_zero_length_part_is_refused_before_the_session_is_touched(rig: Rig) -> None:
    session = await rig.service.open(rig.drive_id, rig.folder_id, b"z.bin", declared_size=4)
    with pytest.raises(InvalidRequest) as raised:
        await rig.service.put_part(session.id, 1, _stream(b""), size=0, checksum=_digest(b""))
    assert raised.value.code == "files.empty_part"
    assert (await rig.service.status(session.id)).state == "open"


async def test_an_identical_resend_is_a_no_op_that_never_reaches_the_store_again(
    rig: Rig,
) -> None:
    session = await rig.service.open(rig.drive_id, rig.folder_id, b"idem.bin", declared_size=4)
    digest = _digest(b"abcd")
    await rig.service.put_part(session.id, 1, _stream(b"abcd"), size=4, checksum=digest)
    puts = [call for call in rig.store.calls if call.method == "put"]
    again = await rig.service.put_part(session.id, 1, _stream(b"abcd"), size=4, checksum=digest)
    assert again.duplicate is True
    assert [call for call in rig.store.calls if call.method == "put"] == puts
    status = await rig.service.status(session.id)
    assert status.offset == 4
    assert status.accepted_parts == (1,)


async def test_a_resend_with_a_different_checksum_is_refused_and_changes_nothing(
    rig: Rig,
) -> None:
    session = await rig.service.open(rig.drive_id, rig.folder_id, b"mis.bin", declared_size=4)
    await rig.service.put_part(session.id, 1, _stream(b"abcd"), size=4, checksum=_digest(b"abcd"))
    with pytest.raises(Conflict) as raised:
        await rig.service.put_part(
            session.id, 1, _stream(b"WXYZ"), size=4, checksum=_digest(b"WXYZ")
        )
    assert raised.value.code == "files.part_mismatch"
    assert rig.object_path(session.id, 1, b"abcd").read_bytes() == b"abcd"
    assert (await rig.service.status(session.id)).offset == 4


async def test_a_session_in_committing_accepts_no_further_parts(rig: Rig) -> None:
    session = await rig.service.open(rig.drive_id, rig.folder_id, b"c.bin", declared_size=4)
    await rig.repo.session.execute(
        update(FileUploadSession.__table__)
        .where(FileUploadSession.id == session.id)
        .values(state="committing")
    )
    await rig.repo.session.commit()
    with pytest.raises(Conflict) as raised:
        await rig.service.put_part(
            session.id, 1, _stream(b"abcd"), size=4, checksum=_digest(b"abcd")
        )
    assert raised.value.code == "files.session_state"
    assert rig.store.calls == []


async def test_a_part_for_an_unknown_session_is_not_found(rig: Rig) -> None:
    with pytest.raises(NotFound):
        await rig.service.put_part(
            SessionId(uuid.uuid4()), 1, _stream(b"a"), size=1, checksum=_digest(b"a")
        )


# ---- resume -------------------------------------------------------------


async def test_a_client_killed_at_part_seven_resumes_with_exactly_the_accepted_parts(
    rig: Rig,
) -> None:
    """The crash lands between the store write and the part row, so the
    part is *not* accepted; status reports 1..6, the remaining parts complete,
    and every object on disk holds the bytes its part declared."""
    total = 20
    payloads = {n: f"part-{n:02d}".encode() * 4 for n in range(1, total + 1)}
    declared = sum(len(p) for p in payloads.values())
    session = await rig.service.open(
        rig.drive_id, rig.folder_id, b"resume.bin", declared_size=declared
    )

    async def send(part_no: int) -> None:
        payload = payloads[part_no]
        await rig.service.put_part(
            session.id,
            part_no,
            _stream(payload),
            size=len(payload),
            checksum=_digest(payload),
        )

    for part_no in range(1, 7):
        await send(part_no)
    # The kill is a *different* client process: its checkpoints die with it,
    # and the resuming client below is the rig's own service.
    killer = PausingCheckpoints()
    killer.kill("uploads.after_part_stored")
    dying = UploadService(
        rig.repo,
        _ctx(),
        FakeClock(now=EPOCH),
        cast(DomainStore, rig.store_handle),
        part_size=SMALL_PART,
        checkpoints=killer,
    )
    payload = payloads[7]
    with pytest.raises(CheckpointKilled):
        await dying.put_part(
            session.id, 7, _stream(payload), size=len(payload), checksum=_digest(payload)
        )

    resumed = await rig.service.status(session.id)
    assert resumed.accepted_parts == tuple(range(1, 7))
    assert resumed.offset == sum(len(payloads[n]) for n in range(1, 7))
    assert resumed.complete is False

    for part_no in range(7, total + 1):
        await send(part_no)
    final = await rig.service.status(session.id)
    assert final.accepted_parts == tuple(range(1, total + 1))
    assert final.offset == declared
    assert final.complete is True
    for part_no, payload in payloads.items():
        assert rig.object_path(session.id, part_no, payload).read_bytes() == payload


# ---- races --------------------------------------------------------------


async def test_two_backends_sending_the_same_part_leave_exactly_one_row(
    rig: Rig, files_engine: AsyncEngine
) -> None:
    """Both miss the pre-read, both store, and the composite key decides: one
    row, one accepted result, one duplicate, and the offset counted once."""
    session = await rig.service.open(rig.drive_id, rig.folder_id, b"race.bin", declared_size=4)
    other, other_session = await _second_backend(rig, files_engine)
    digest = _digest(b"abcd")

    rig.checkpoints.pause("uploads.before_part_row")
    first = asyncio.create_task(
        rig.service.put_part(session.id, 1, _stream(b"abcd"), size=4, checksum=digest)
    )
    await rig.checkpoints.wait_paused("uploads.before_part_row")
    second = asyncio.create_task(
        other.put_part(session.id, 1, _stream(b"abcd"), size=4, checksum=digest)
    )
    await asyncio.sleep(0)
    rig.checkpoints.release("uploads.before_part_row")
    results = await asyncio.gather(first, second)
    await other_session.close()

    rows = await rig.repo.session.execute(
        select(func.count())
        .select_from(FileUploadPart)
        .where(FileUploadPart.session_id == session.id)
    )
    assert int(rows.scalar_one()) == 1
    assert sorted(result.duplicate for result in results) == [False, True]
    assert (await rig.service.status(session.id)).offset == 4


async def test_abort_racing_a_part_is_decided_by_the_state_machine(
    rig: Rig, files_engine: AsyncEngine
) -> None:
    """Two backends, one session: the abort's compare-and-swap is what ends it.

    Whichever transaction the row lock admits first, the session ends
    ``aborted`` with no hold, and the next part is refused on the state — the
    predicate in the claim is the only thing standing between an aborted
    session and bytes still landing in it.
    """
    session = await rig.service.open(rig.drive_id, rig.folder_id, b"ab.bin", declared_size=8)
    other, other_session = await _second_backend(rig, files_engine)
    rig.checkpoints.pause("uploads.after_part_stored")
    part = asyncio.create_task(
        rig.service.put_part(session.id, 1, _stream(b"abcd"), size=4, checksum=_digest(b"abcd"))
    )
    await rig.checkpoints.wait_paused("uploads.after_part_stored")
    aborting = asyncio.create_task(other.abort(session.id))
    await asyncio.sleep(0)
    rig.checkpoints.release("uploads.after_part_stored")
    await asyncio.gather(part, aborting, return_exceptions=True)
    await other_session.close()

    status = await rig.service.status(session.id)
    assert status.state == "aborted"
    assert await _held(rig.repo.session, rig.drive_uuid) == 0
    with pytest.raises(Conflict) as raised:
        await rig.service.put_part(
            session.id, 2, _stream(b"efgh"), size=4, checksum=_digest(b"efgh")
        )
    assert raised.value.code == "files.session_state"


# ---- streaming holds nothing -------------------------------------------

#: How long a part may wait for another part of its upload. Far above a claim
#: and a row write, far below forever: the parts used to queue on the session
#: row for as long as the first one took to stream.
SIDE_BY_SIDE_SECONDS = 10.0


class _HeldOpen:
    """A part's body that sends half its bytes and then waits to be released,
    the way a slow client keeps a part streaming."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def body(self) -> AsyncIterator[bytes]:
        half = len(self.payload) // 2
        yield self.payload[:half]
        self.started.set()
        await self.release.wait()
        yield self.payload[half:]


async def test_the_parts_of_one_upload_stream_side_by_side(
    rig: Rig, files_engine: AsyncEngine
) -> None:
    """A part still streaming holds no lock the next part needs.

    Part 1 is held mid-stream on one backend; part 2 is sent whole on another
    and must be accepted before part 1 finishes. The claim used to hold the
    session row for the length of the stream, so every other part of a large
    upload queued behind it and timed out as a 503.
    """
    first_bytes, second_bytes = b"abcdefgh", b"ijklmnop"
    session = await rig.service.open(rig.drive_id, rig.folder_id, b"wide.bin", declared_size=16)
    other, other_session = await _second_backend(rig, files_engine)
    held = _HeldOpen(first_bytes)
    first = asyncio.create_task(
        rig.service.put_part(session.id, 1, held.body(), size=8, checksum=_digest(first_bytes))
    )
    try:
        await asyncio.wait_for(held.started.wait(), SIDE_BY_SIDE_SECONDS)
        second = await asyncio.wait_for(
            other.put_part(
                session.id, 2, _stream(second_bytes), size=8, checksum=_digest(second_bytes)
            ),
            SIDE_BY_SIDE_SECONDS,
        )
        assert second.duplicate is False
        assert not first.done(), "part 1 finished before it was released"
    finally:
        held.release.set()
        await asyncio.gather(first, return_exceptions=True)
        await other_session.close()
    assert first.result().duplicate is False

    status = await rig.service.status(session.id)
    assert status.accepted_parts == (1, 2)
    assert status.offset == 16
    assert rig.object_path(session.id, 1, first_bytes).read_bytes() == first_bytes
    assert rig.object_path(session.id, 2, second_bytes).read_bytes() == second_bytes


async def test_different_bytes_racing_for_one_part_never_share_an_object(
    rig: Rig, files_engine: AsyncEngine
) -> None:
    """Two requests stream different bytes for part 1 at once; the row decides.

    With no lock held while a part streams, both writes are in flight together.
    The one whose row lands first is the part, its bytes are exactly the object
    its row names, and the loser is refused and leaves nothing behind.
    """
    slow_bytes, fast_bytes = b"abcd", b"WXYZ"
    session = await rig.service.open(rig.drive_id, rig.folder_id, b"one.bin", declared_size=4)
    other, other_session = await _second_backend(rig, files_engine)
    held = _HeldOpen(slow_bytes)
    slow = asyncio.create_task(
        rig.service.put_part(session.id, 1, held.body(), size=4, checksum=_digest(slow_bytes))
    )
    try:
        await asyncio.wait_for(held.started.wait(), SIDE_BY_SIDE_SECONDS)
        fast = await asyncio.wait_for(
            other.put_part(
                session.id, 1, _stream(fast_bytes), size=4, checksum=_digest(fast_bytes)
            ),
            SIDE_BY_SIDE_SECONDS,
        )
    finally:
        held.release.set()
        outcome = (await asyncio.gather(slow, return_exceptions=True))[0]
        await other_session.close()

    assert fast.duplicate is False
    assert isinstance(outcome, Conflict) and outcome.code == "files.part_mismatch"
    assert rig.object_path(session.id, 1, fast_bytes).read_bytes() == fast_bytes
    assert not rig.object_path(session.id, 1, slow_bytes).exists()
    assert (await rig.service.status(session.id)).offset == 4


# ---- abort --------------------------------------------------------------


async def test_abort_releases_the_hold_and_drops_the_staged_objects(rig: Rig) -> None:
    session = await rig.service.open(rig.drive_id, rig.folder_id, b"gone.bin", declared_size=4)
    await rig.service.put_part(session.id, 1, _stream(b"abcd"), size=4, checksum=_digest(b"abcd"))
    assert await _held(rig.repo.session, rig.drive_uuid) == 4

    await rig.service.abort(session.id)

    assert await _held(rig.repo.session, rig.drive_uuid) == 0
    assert (await rig.service.status(session.id)).state == "aborted"
    assert not rig.object_path(session.id, 1, b"abcd").exists()


async def test_a_second_abort_is_refused_because_the_session_is_terminal(rig: Rig) -> None:
    session = await rig.service.open(rig.drive_id, rig.folder_id, b"twice.bin", declared_size=4)
    await rig.service.abort(session.id)
    with pytest.raises(Conflict) as raised:
        await rig.service.abort(session.id)
    assert raised.value.code == "files.session_state"


async def test_abort_of_an_unknown_session_is_not_found(rig: Rig) -> None:
    with pytest.raises(NotFound):
        await rig.service.abort(SessionId(uuid.uuid4()))

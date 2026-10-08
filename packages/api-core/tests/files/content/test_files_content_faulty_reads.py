"""A store that lies is caught before a caller sees a byte it cannot vouch for.

Three ways an object store misbehaves without ever saying so: it rots a bit in
flight, it hands back a short read, or it takes the write and then denies the
object exists. None of them raises on its own, so each one is scheduled on a
real driver here and the assertion is about what the *consumer* received — the
bytes it actually collected before the failure — rather than about which error
the wrapper threw.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.content import HEAD_RETRY_DELAY, HEAD_WINDOW, ContentService
from alkera_core.files.errors import StoreUnavailable
from alkera_core.files.hashing import BLOCK_BYTES
from alkera_core.files.ids import DomainId, NodeId, OrgScope, VersionId
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.errors import ChecksumMismatch, StoreError
from alkera_core.files.store.errors import NotFound as StoreNotFound
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.uploads import FileUploadSession
from alkera_core.models.files.versions import FileVersion
from alkera_test_support.files.faulty_store import Fault, FaultSchedule, FaultyStore
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files.content.conftest import content_ctx, stream

#: Big enough for two whole blocks plus a tail, so "the first block survived and
#: the second did not" is a statement about two separate verified reads.
PAYLOAD_BYTES = 2 * BLOCK_BYTES + 512 * 1024
FLIP_AT = 5 * 1024 * 1024
"""The absolute offset of the rotted byte: 1 MiB into the second block."""

#: How many retries fit inside the window before the deadline is reached.
MAX_RETRIES = int(HEAD_WINDOW / HEAD_RETRY_DELAY)


def payload_of(size: int) -> bytes:
    """Non-uniform bytes: a truncation or a flip cannot coincide with the original."""
    return (bytes(range(256)) * (size // 256 + 1))[:size]


@dataclass(frozen=True, slots=True)
class FaultyRig:
    service: ContentService
    store: FaultyStore
    session: AsyncSession
    repo: FilesRepo
    node: FileNode
    drive_uuid: uuid.UUID


async def build_rig(
    *,
    faults: tuple[Fault, ...],
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    checkpoints: PausingCheckpoints,
    tmp_path: Path,
) -> FaultyRig:
    """The content rig, but with a fault schedule the caller chose.

    The shared ``content_rig`` fixture is deliberately fault-free; a scheduled
    fault has to be decided per test, so this builds the same stack — real
    Postgres, a real filesystem store — with the schedule installed.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("papers/ papers/a.bin", drive=drive)
    domain_id = DomainId(drive.dedup_domain_id)
    inner = FaultyStore(
        FilesystemStore(tmp_path / "domains" / str(domain_id), clock=clock.now),
        FaultSchedule(faults=faults),
    )
    repo = FilesRepo(files_session, OrgScope(org_team_id=files_org.org_team_id))
    return FaultyRig(
        service=ContentService(
            repo,
            content_ctx(),
            clock,
            _RootedDomainStore(inner, domain_id),
            checkpoints=checkpoints,
        ),
        store=inner,
        session=files_session,
        repo=repo,
        node=tree["papers/a.bin"],
        drive_uuid=drive.id,
    )


async def collect(rig: FaultyRig, version_id: VersionId) -> tuple[bytes, BaseException | None]:
    """Everything the consumer received, and whatever ended the stream."""
    received = bytearray()
    try:
        async for chunk in await rig.service.open(version_id):
            received.extend(chunk)
    except Exception as exc:
        return bytes(received), exc
    return bytes(received), None


# ---- corrupted reads --------------------------------------------------------


@pytest.fixture
def write_payload(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    checkpoints: PausingCheckpoints,
    tmp_path: Path,
) -> Callable[[tuple[Fault, ...]], Awaitable[tuple[FaultyRig, VersionId, bytes]]]:
    """Write one clean object into a rig whose read faults are already scheduled.

    Scheduling before the write is safe and deliberate: a read fault can only
    land on ``get`` and a put never issues one, so the bytes on disk are known
    good and any damage the reader sees came from the schedule.
    """

    async def write(faults: tuple[Fault, ...]) -> tuple[FaultyRig, VersionId, bytes]:
        rig = await build_rig(
            faults=faults,
            files_session=files_session,
            files_org=files_org,
            files_factory=files_factory,
            clock=clock,
            checkpoints=checkpoints,
            tmp_path=tmp_path,
        )
        payload = payload_of(PAYLOAD_BYTES)
        info = await rig.service.put_version(
            NodeId(rig.node.id),
            stream(payload, chunk=256 * 1024),
            size_declared=len(payload),
            if_match=rig.node.etag,
        )
        return rig, info.id, payload

    return write


WritePayload = Callable[[tuple[Fault, ...]], Awaitable[tuple[FaultyRig, VersionId, bytes]]]


async def test_a_flipped_bit_stops_the_stream_at_the_block_that_holds_it(
    write_payload: WritePayload,
) -> None:
    # Aimed at the second block read: 1 MiB into it is absolute offset 5 MiB.
    rig, version_id, payload = await write_payload(
        (
            Fault(
                kind="bit_flip",
                key_prefix="objects/",
                call_index=1,
                offset=FLIP_AT - BLOCK_BYTES,
            ),
        )
    )

    received, failure = await collect(rig, version_id)

    assert isinstance(failure, ChecksumMismatch)
    # The first block verified, so it was handed over; the second never was, so
    # the consumer holds no byte from the block the flip landed in.
    assert received == payload[:BLOCK_BYTES]
    assert len(received) <= FLIP_AT
    assert rig.store.unfired() == []


async def test_a_truncated_read_yields_nothing_at_all(
    write_payload: WritePayload,
) -> None:
    rig, version_id, _ = await write_payload(
        (
            Fault(
                kind="truncated_read",
                key_prefix="objects/",
                call_index=0,
                bytes_before_failure=1024 * 1024,
            ),
        )
    )

    received, failure = await collect(rig, version_id)

    assert isinstance(failure, ChecksumMismatch)
    assert received == b""


async def test_an_undamaged_read_returns_every_byte(
    write_payload: WritePayload,
) -> None:
    # The negative twin: the same rig with no fault scheduled has to succeed, so
    # the two failures above cannot be the rig refusing everything.
    rig, version_id, payload = await write_payload(())

    received, failure = await collect(rig, version_id)

    assert failure is None
    assert received == payload


# ---- delayed visibility -----------------------------------------------------


async def versions(rig: FaultyRig) -> int:
    rows = await rig.session.execute(
        select(func.count()).select_from(FileVersion).where(FileVersion.node_id == rig.node.id)
    )
    return int(rows.scalar_one())


async def session_states(rig: FaultyRig) -> list[str]:
    rows = await rig.session.execute(
        select(FileUploadSession.state).where(FileUploadSession.drive_id == rig.drive_uuid)
    )
    return sorted(rows.scalars().all())


async def put_with_delayed_head(
    denials: int,
    *,
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    checkpoints: PausingCheckpoints,
    tmp_path: Path,
) -> tuple[FaultyRig, BaseException | None]:
    rig = await build_rig(
        faults=(Fault(kind="delayed_visibility", key_prefix="objects/", count=denials),),
        files_session=files_session,
        files_org=files_org,
        files_factory=files_factory,
        clock=clock,
        checkpoints=checkpoints,
        tmp_path=tmp_path,
    )
    payload = payload_of(256 * 1024)
    try:
        await rig.service.put_version(
            NodeId(rig.node.id),
            stream(payload, chunk=64 * 1024),
            size_declared=len(payload),
            if_match=rig.node.etag,
        )
    except Exception as exc:
        return rig, exc
    return rig, None


@pytest.mark.parametrize(
    "denials",
    [pytest.param(3, id="three-denials"), pytest.param(MAX_RETRIES, id="last-retry-in-window")],
)
async def test_a_head_that_lags_inside_the_window_is_waited_out(
    denials: int,
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    checkpoints: PausingCheckpoints,
    tmp_path: Path,
) -> None:
    rig, failure = await put_with_delayed_head(
        denials,
        files_session=files_session,
        files_org=files_org,
        files_factory=files_factory,
        clock=clock,
        checkpoints=checkpoints,
        tmp_path=tmp_path,
    )

    assert failure is None
    assert await versions(rig) == 1
    assert await session_states(rig) == ["done"]
    # The wait was spent on the injected clock, one retry step per denial.
    assert clock.monotonic() == pytest.approx(denials * HEAD_RETRY_DELAY.total_seconds())
    assert rig.store.unfired() == []


async def test_a_head_that_lags_past_the_window_fails_closed(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    checkpoints: PausingCheckpoints,
    tmp_path: Path,
) -> None:
    rig, failure = await put_with_delayed_head(
        MAX_RETRIES + 1,
        files_session=files_session,
        files_org=files_org,
        files_factory=files_factory,
        clock=clock,
        checkpoints=checkpoints,
        tmp_path=tmp_path,
    )

    # Never optimistic: one denial past the window is a refusal, not a version
    # written on the hope that the bytes will turn up.
    assert isinstance(failure, StoreNotFound)
    assert await versions(rig) == 0
    assert await session_states(rig) == ["aborted"]
    await rig.session.refresh(rig.node)
    assert rig.node.head_version_id is None


# ---- a row whose recorded digests do not cover its own bytes ----------------


async def plant_block_hashes(rig: FaultyRig, version_id: VersionId, digests: list[str]) -> None:
    """Rewrite what the row claims to know about its own blocks.

    The bytes on disk are untouched: this is the metadata half of the same
    corruption a torn write leaves behind, and a reader is not allowed to tell
    the two apart — it has to refuse either way.
    """
    version = await rig.session.get(FileVersion, version_id)
    assert version is not None
    version.version_metadata = {**version.version_metadata, "block_hashes": digests}
    await rig.session.flush()


async def recorded_digests(rig: FaultyRig, version_id: VersionId) -> list[str]:
    version = await rig.session.get(FileVersion, version_id)
    assert version is not None
    return list(version.version_metadata["block_hashes"])


async def test_a_short_digest_tuple_refuses_the_read_instead_of_waving_the_tail_through(
    write_payload: WritePayload,
) -> None:
    """A version that records fewer digests than it has blocks is unreadable.

    ``PAYLOAD_BYTES`` spans three blocks; keeping only the first digest leaves
    two blocks with nothing to check them against. Checking what is recorded and
    yielding the rest is exactly how a truncated — or never written — metadata
    row turns into unverified bytes on a caller's disk, so the whole read is
    refused, including the one block whose digest is still right.
    """
    rig, version_id, _ = await write_payload(())
    assert len(await recorded_digests(rig, version_id)) == 3
    await plant_block_hashes(rig, version_id, (await recorded_digests(rig, version_id))[:1])

    received, failure = await collect(rig, version_id)

    assert isinstance(failure, ChecksumMismatch)
    assert "1 recorded block digests for 3 blocks" in str(failure)
    assert received == b"", "no byte may be handed over from a version we cannot check"


async def test_a_version_with_no_recorded_digests_is_refused(
    write_payload: WritePayload,
) -> None:
    """The shape a restore used to write: no ``block_hashes`` at all reads as nothing."""
    rig, version_id, _ = await write_payload(())
    await plant_block_hashes(rig, version_id, [])

    received, failure = await collect(rig, version_id)

    assert isinstance(failure, ChecksumMismatch)
    assert received == b""


async def test_an_inline_version_missing_its_digests_is_refused(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    checkpoints: PausingCheckpoints,
    tmp_path: Path,
) -> None:
    """Inline bytes get the same treatment: the row is not its own witness."""
    rig = await build_rig(
        faults=(),
        files_session=files_session,
        files_org=files_org,
        files_factory=files_factory,
        clock=clock,
        checkpoints=checkpoints,
        tmp_path=tmp_path,
    )
    payload = payload_of(1024)
    info = await rig.service.put_version(
        NodeId(rig.node.id),
        stream(payload),
        size_declared=len(payload),
        if_match=rig.node.etag,
    )
    await plant_block_hashes(rig, info.id, [])

    received, failure = await collect(rig, info.id)

    assert isinstance(failure, ChecksumMismatch)
    assert received == b""


# ---- refusals the library never named one by one ----------------------------


class _RefusingReads:
    """A driver whose named methods fail with the *base* ``StoreError``.

    ``Throttled`` and ``Unavailable`` are the two classes the library names by
    hand; every other normalized one — an expired scoped credential, a denied
    bucket, a bucket that was never created — arrived as something no byte path
    caught, so it escaped the Files vocabulary as a platform 500. Raising the
    base class is the pin: it can only pass if the catch is on ``StoreError``.
    """

    def __init__(self, inner: object, *methods: str) -> None:
        self._inner = inner
        self._methods = frozenset(methods)

    def __getattr__(self, name: str) -> object:
        if name in self._methods:

            async def _refuse(*_args: object, **_kwargs: object) -> object:
                raise StoreError(f"{name}: this deployment's credential expired")

            return _refuse
        return getattr(self._inner, name)


async def refusing_service(rig: FaultyRig, clock: FakeClock, *methods: str) -> ContentService:
    """``rig``'s service again, with its driver refusing ``methods`` outright."""
    async with rig.repo.transaction():
        drive = await rig.repo.drive_for_org()
    assert drive is not None
    return ContentService(
        rig.repo,
        content_ctx(),
        clock,
        _RootedDomainStore(_RefusingReads(rig.store, *methods), DomainId(drive.dedup_domain_id)),
    )


async def test_an_unmapped_refusal_on_a_read_leaves_in_the_files_vocabulary(
    write_payload: WritePayload,
    clock: FakeClock,
) -> None:
    """A ``get`` that fails for a reason the library does not name one by one
    used to leave as a raw driver exception the caller cannot tell from a bug."""
    rig, version_id, _ = await write_payload(())
    service = await refusing_service(rig, clock, "get")

    received = bytearray()
    with pytest.raises(StoreUnavailable) as refusal:
        async for chunk in await service.open(version_id):
            received.extend(chunk)

    assert refusal.value.code == "files.store_unavailable"
    assert not received, "nothing was handed to the consumer before the refusal"


async def test_an_unmapped_refusal_on_a_head_leaves_in_the_files_vocabulary(
    write_payload: WritePayload,
    clock: FakeClock,
) -> None:
    """The same for the ``head`` the publish path runs to prove the object is
    visible before a version is allowed to point at it."""
    rig, _, _ = await write_payload(())
    service = await refusing_service(rig, clock, "head")
    async with rig.repo.transaction():
        node = await rig.repo.node(NodeId(rig.node.id))
    assert node is not None

    payload = payload_of(PAYLOAD_BYTES - 1)
    with pytest.raises(StoreUnavailable) as refusal:
        await service.put_version(
            NodeId(rig.node.id),
            stream(payload, chunk=256 * 1024),
            size_declared=len(payload),
            if_match=node.etag,
        )

    assert refusal.value.code == "files.store_unavailable"

"""Completing an upload: the part agreement, the promote step, the replay.

Everything runs against real Postgres on this run's lane database and a real
``FilesystemStore``, so "the bytes are there" is read back off the disk and
"the session went back to ``uploading``" is read off the row rather than
inferred. ``complete`` is asserted to hand back an operation on both paths —
fresh bytes and bytes the org already holds — because the moment it returned a
synchronous result on one of them, the wait would tell a caller which.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alkera_core.authz.principal import ActingContext, Principal, PrincipalKind
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.errors import Conflict, NotFound, PreconditionFailed
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.ids import DomainId, DriveId, NodeId, OrgScope, SessionId
from alkera_core.files.leases import Lease, LeaseConflict, LeaseContext, LeaseService
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.errors import StoreError
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.files.uploads import (
    PartRef,
    UploadCompletion,
    UploadService,
    legacy_part_key,
    part_key,
)
from alkera_core.models.files.uploads import FileUploadSession
from alkera_test_support.files.faulty_store import FaultSchedule, FaultyStore
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.fencing import held_by

#: Small enough that a handful of parts crosses several part boundaries.
SMALL_PART = 8


def _ctx() -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER, id=str(uuid.uuid4()), org_id=uuid.uuid4()
        )
    )


def _digest(payload: bytes) -> bytes:
    return hash_bytes(payload).content_hash


async def _stream(payload: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(payload), 5):
        yield payload[start : start + 5]


@dataclass(frozen=True, slots=True)
class Rig:
    """A drive, a folder, the two halves of the upload path, and the store."""

    uploads: UploadService
    completion: UploadCompletion
    #: The one principal this rig acts as. A lease a test takes is taken by the
    #: same caller that opens the session under it, because that is the only
    #: shape a request has — and the fence matches the holder on the lease row.
    ctx: ActingContext
    repo: FilesRepo
    root: Path
    domain_id: DomainId
    drive_uuid: uuid.UUID
    folder_uuid: uuid.UUID
    session: AsyncSession
    clock: FakeClock
    store: FaultyStore

    @property
    def drive_id(self) -> DriveId:
        return DriveId(self.drive_uuid)

    @property
    def folder_id(self) -> NodeId:
        return NodeId(self.folder_uuid)

    def part_path(self, session_id: SessionId, ref: PartRef) -> Path:
        key = part_key(session_id, ref.part_no, bytes(ref.checksum))
        return self.root / "domains" / str(self.domain_id) / key

    async def upload(self, name: bytes, payload: bytes) -> tuple[SessionId, list[PartRef]]:
        """Open a session and send ``payload`` as ``SMALL_PART``-sized parts."""
        opened = await self.uploads.open(
            self.drive_id, self.folder_id, name, declared_size=len(payload)
        )
        session_id = SessionId(opened.id)
        refs: list[PartRef] = []
        for index in range(0, len(payload), SMALL_PART):
            chunk = payload[index : index + SMALL_PART]
            part_no = index // SMALL_PART + 1
            await self.uploads.put_part(
                session_id,
                part_no,
                _stream(chunk),
                size=len(chunk),
                checksum=_digest(chunk),
            )
            refs.append(PartRef(part_no=part_no, size=len(chunk), checksum=_digest(chunk)))
        return session_id, refs

    async def state(self, session_id: SessionId) -> str:
        async with self.repo.transaction():
            statement = select(FileUploadSession.state).where(FileUploadSession.id == session_id)
            return str((await self.repo.execute_scoped(statement)).scalar_one())

    async def hold(self, session_id: SessionId) -> int:
        async with self.repo.transaction():
            statement = select(FileUploadSession.quota_hold_bytes).where(
                FileUploadSession.id == session_id
            )
            return int((await self.repo.execute_scoped(statement)).scalar_one())

    async def child_names(self) -> list[bytes]:
        async with self.repo.transaction():
            rows = await self.repo.siblings(self.folder_id)
        return sorted(bytes(row.name) for row in rows)


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
    domain_root = tmp_path / "domains" / str(domain_id)
    inner = FaultyStore(FilesystemStore(domain_root, clock=clock.now), FaultSchedule(faults=()))
    store = _RootedDomainStore(inner, domain_id)
    repo = FilesRepo(files_session, OrgScope(org_team_id=files_org.org_team_id))
    ctx = _ctx()
    return Rig(
        uploads=UploadService(repo, ctx, clock, store, part_size=SMALL_PART),
        completion=UploadCompletion(repo, ctx, clock, store, part_size=SMALL_PART),
        ctx=ctx,
        repo=repo,
        root=tmp_path,
        domain_id=domain_id,
        drive_uuid=drive.id,
        folder_uuid=tree["uploads"].id,
        session=files_session,
        clock=clock,
        store=inner,
    )


async def _read(rig: Rig, node_id: NodeId) -> bytes:
    """The node's head bytes, as a reader would get them."""
    from alkera_core.files.content import ContentService

    async with rig.repo.transaction():
        node = await rig.repo.node(node_id)
        assert node is not None and node.head_version_id is not None
        version_id = node.head_version_id
    content = ContentService(
        rig.repo,
        _ctx(),
        rig.clock,
        _RootedDomainStore(
            FilesystemStore(rig.root / "domains" / str(rig.domain_id), clock=rig.clock.now),
            rig.domain_id,
        ),
    )
    from alkera_core.files.ids import VersionId

    stream = await content.open(VersionId(version_id))
    return b"".join([chunk async for chunk in stream])


async def test_a_twenty_part_upload_completes_byte_identical(rig: Rig) -> None:
    """The concatenation is the file: 20 parts in, the same bytes out."""
    payload = bytes(range(256))[:160]
    assert len(payload) // SMALL_PART == 20
    session_id, refs = await rig.upload(b"twenty.bin", payload)

    operation = await rig.completion.complete(session_id, refs)

    assert operation.state == "queued", "complete must hand back work, never a finished result"
    assert await rig.state(session_id) == "committing"
    await rig.completion.promote(session_id, operation.id)
    settled = await rig.completion._ops.get(operation.id)
    assert settled.state == "done"
    assert settled.result_node_id is not None
    assert await _read(rig, NodeId(settled.result_node_id)) == payload
    assert await rig.state(session_id) == "done"
    assert await rig.hold(session_id) == 0


async def test_a_session_staged_under_the_old_part_keys_still_completes(rig: Rig) -> None:
    """Sessions live for days, so one staged by the previous build — each part
    under its number alone — is completed by this one, byte for byte."""
    payload = b"staged by the build before"
    session_id, refs = await rig.upload(b"old-keys.bin", payload)
    for ref in refs:
        old = rig.root / "domains" / str(rig.domain_id) / legacy_part_key(session_id, ref.part_no)
        rig.part_path(session_id, ref).rename(old)

    operation = await rig.completion.complete(session_id, refs)
    await rig.completion.promote(session_id, operation.id)

    settled = await rig.completion._ops.get(operation.id)
    assert settled.state == "done"
    assert settled.result_node_id is not None
    assert await _read(rig, NodeId(settled.result_node_id)) == payload


async def test_a_promote_runs_from_its_operation_row_alone(rig: Rig) -> None:
    """``complete`` binds the session onto the row it queues, so the worker, the
    inline dispatch and a recovery of a lost hand-off all start a promote from
    the operation id: no runner needs the session handed to it separately."""
    payload = b"the row alone names the upload"
    session_id, refs = await rig.upload(b"from-the-row.bin", payload)
    operation = await rig.completion.complete(session_id, refs)

    await rig.completion.promote_operation(operation.id)

    settled = await rig.completion._ops.get(operation.id)
    assert settled.state == "done"
    assert settled.result["session_id"] == str(session_id)
    assert b"from-the-row.bin" in await rig.child_names()


async def test_an_operation_that_names_no_session_is_refused_not_guessed(rig: Rig) -> None:
    """An ``upload`` row with no bound session has nothing to commit; the
    promote refuses by name rather than reaching for some other session, and
    the row ends ``failed`` so the recovery pass does not offer it forever."""
    orphan = await rig.completion._ops.start("upload", drive_id=rig.drive_id)

    with pytest.raises(Conflict) as refused:
        await rig.completion.promote_operation(orphan.id)
    assert refused.value.code == "files.session_state"
    settled = await rig.completion._ops.get(orphan.id)
    assert settled.state == "failed"
    assert settled.errors


@pytest.mark.parametrize("gone", ["expired", "aborted"])
async def test_a_recovered_upload_whose_session_is_gone_ends_failed_with_its_reason(
    rig: Rig, gone: str
) -> None:
    """A lost promote hand-off is recovered from its row, possibly long after
    the upload. If the session is no longer committing by then -- expired or
    aborted by the sweeper -- the promote must end the operation ``failed``
    with the reason on the row. Left ``queued`` it would be found by the
    recovery pass on every tick, forever; the pass's query no longer sees it.
    A committing session does not TTL-expire (the sweeper only expires
    part-accepting states), so the state is set the way the sweeper leaves it."""
    from datetime import UTC, datetime, timedelta

    session_id, refs = await rig.upload(b"late.bin", b"arrived too late")
    operation = await rig.completion.complete(session_id, refs)
    async with rig.repo.transaction():
        await rig.session.execute(
            text("UPDATE file_upload_sessions SET state = :s WHERE id = :id"),
            {"s": gone, "id": session_id},
        )

    with pytest.raises(Conflict) as refused:
        await rig.completion.promote_operation(operation.id)

    assert refused.value.code == "files.session_state"
    settled = await rig.completion._ops.get(operation.id)
    assert settled.state == "failed"
    assert any(gone in str(error) for error in settled.errors), settled.errors
    assert b"late.bin" not in await rig.child_names()
    later = datetime.now(UTC) + timedelta(days=1)
    found = await rig.completion._ops.abandoned_queued(later, limit=100)
    assert operation.id not in {entry.id for entry in found}


async def test_a_promote_declares_its_bytes_and_the_watchdog_waits_for_them(rig: Rig) -> None:
    """A large landing is neither invisible nor dead.

    ``complete`` answers 202 and the promote runs behind it, so the operation
    row is the only place a caller can watch the bytes land — and it is the
    only evidence the watchdog has that the runner is alive. The promote
    therefore says how many bytes it owes before it moves one, and the wait a
    silent operation is given grows with that figure instead of being the flat
    floor a rename is judged on.
    """
    payload = bytes(range(256))[:160]
    session_id, refs = await rig.upload(b"declared.bin", payload)
    operation = await rig.completion.complete(session_id, refs)

    await rig.completion.promote(session_id, operation.id)

    settled = await rig.completion._ops.get(operation.id)
    assert settled.state == "done"
    async with rig.repo.transaction():
        progress = (
            await rig.repo.session.execute(
                text("SELECT progress FROM file_ops WHERE id = :id"), {"id": operation.id}
            )
        ).scalar_one()
    assert progress["byte_budget"] == len(payload), (
        "the promote must publish the bytes it owes, or the watchdog judges a "
        "terabyte on the ten minutes a rename gets"
    )
    assert progress["bytes"] == 0, "a payload under the publish threshold beats once, at the start"


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        pytest.param(lambda refs: refs[:-1], "missing", id="a-part-the-server-holds-is-missing"),
        pytest.param(
            lambda refs: [*refs, PartRef(part_no=99, size=8, checksum=b"\x00" * 32)],
            "extra",
            id="a-part-the-server-never-accepted-is-claimed",
        ),
        pytest.param(
            lambda refs: [
                PartRef(part_no=refs[0].part_no, size=refs[0].size, checksum=b"\x01" * 32),
                *refs[1:],
            ],
            "checksum",
            id="a-part-is-claimed-with-a-different-checksum",
        ),
    ],
)
async def test_a_disagreeing_part_list_is_refused_and_the_session_reopens(
    rig: Rig,
    mutate: object,
    why: str,
) -> None:
    """The refusal is a conflict, and it costs the client nothing but the call."""
    payload = b"abcdefghijklmnop"
    session_id, refs = await rig.upload(b"disagree.bin", payload)

    with pytest.raises(Conflict) as refused:
        await rig.completion.complete(session_id, mutate(refs))  # type: ignore[operator]

    assert refused.value.code == "files.parts_mismatch", why
    assert await rig.state(session_id) == "uploading"
    # The client fixes its list and the same session completes.
    operation = await rig.completion.complete(session_id, refs)
    assert operation.state == "queued"


async def test_the_store_losing_a_part_is_refused_even_though_the_rows_agree(rig: Rig) -> None:
    """The rows are not the truth about bytes; the store is."""
    session_id, refs = await rig.upload(b"lost.bin", b"abcdefghijklmnop")
    rig.part_path(session_id, refs[1]).unlink()

    with pytest.raises(Conflict) as refused:
        await rig.completion.complete(session_id, refs)

    assert refused.value.code == "files.parts_mismatch"
    assert await rig.state(session_id) == "uploading"


async def test_abort_and_complete_cannot_both_take_effect(rig: Rig) -> None:
    """Exactly one of the two lands: the loser is refused by the session state."""
    session_id, refs = await rig.upload(b"race.bin", b"abcdefgh")
    operation = await rig.completion.complete(session_id, refs)
    await rig.uploads.abort(session_id)

    with pytest.raises(Conflict) as promoted:
        await rig.completion.promote(session_id, operation.id)
    assert promoted.value.code == "files.session_state"
    assert await rig.child_names() == [], "the aborted session left no version behind"

    other, other_refs = await rig.upload(b"race2.bin", b"abcdefgh")
    await rig.uploads.abort(other)
    with pytest.raises(Conflict) as completed:
        await rig.completion.complete(other, other_refs)
    assert completed.value.code == "files.session_state"


async def test_a_replayed_promote_never_renames_twice(rig: Rig) -> None:
    """The name is chosen once, on the operation row, not once per attempt."""
    await rig.upload(b"report.pdf", b"original")
    taken, taken_refs = await rig.upload(b"report.pdf", b"original")
    first = await rig.completion.complete(taken, taken_refs)
    await rig.completion.promote(taken, first.id)

    session_id, refs = await rig.upload(b"report.pdf", b"second!!")
    operation = await rig.completion.complete(session_id, refs, conflict="rename")
    # The staged bytes go missing *after* the name was chosen — exactly the
    # window a retried activity lands in.
    staged = rig.part_path(session_id, refs[0])
    withheld = staged.with_suffix(".withheld")
    staged.rename(withheld)
    with pytest.raises(StoreError):
        await rig.completion.promote(session_id, operation.id)
    after_failure = await rig.child_names()
    assert b"report (1).pdf" in after_failure

    withheld.rename(staged)
    await rig.session.execute(
        text("UPDATE file_ops SET state = 'queued' WHERE id = :id"), {"id": operation.id}
    )
    await rig.session.commit()
    await rig.completion.promote(session_id, operation.id)

    assert await rig.child_names() == after_failure, "the retry reused the name it already chose"
    assert b"report (2).pdf" not in after_failure


async def test_a_target_trashed_before_promote_fails_and_leaves_the_object(rig: Rig) -> None:
    """A refusal gives the room back and leaves the bytes for the sweeper."""
    session_id, refs = await rig.upload(b"doomed.bin", b"abcdefgh")
    operation = await rig.completion.complete(session_id, refs)
    await rig.session.execute(
        text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"),
        {"id": rig.folder_uuid},
    )
    await rig.session.commit()

    with pytest.raises(NotFound):
        await rig.completion.promote(session_id, operation.id)

    settled = await rig.completion._ops.get(operation.id)
    assert settled.state == "failed"
    assert await rig.hold(session_id) == 0
    assert rig.part_path(session_id, refs[0]).exists(), (
        "the staged object is the sweeper's to remove"
    )


async def _live_file(rig: Rig, name: bytes) -> tuple[uuid.UUID, int]:
    """The id and etag of the live child called ``name``."""
    async with rig.repo.transaction():
        rows = await rig.repo.siblings(rig.folder_id)
    for row in rows:
        if bytes(row.name) == name:
            return uuid.UUID(str(row.id)), int(row.etag)
    raise AssertionError(f"no live child named {name!r}")


async def _versions(rig: Rig, node_id: uuid.UUID) -> int:
    result = await rig.session.execute(
        text("SELECT count(*) FROM file_versions WHERE node_id = :id"), {"id": node_id}
    )
    return int(result.scalar_one())


async def test_a_replace_commit_lands_a_new_version_on_the_node_that_holds_the_name(
    rig: Rig,
) -> None:
    """``replace`` adopts the live same-name file instead of creating a second one."""
    first, first_refs = await rig.upload(b"report.pdf", b"original")
    await rig.completion.promote(first, (await rig.completion.complete(first, first_refs)).id)
    node_id, _ = await _live_file(rig, b"report.pdf")
    assert await _versions(rig, node_id) == 1

    second, second_refs = await rig.upload(b"report.pdf", b"replaced")
    operation = await rig.completion.complete(second, second_refs, conflict="replace")
    info = await rig.completion.promote(second, operation.id)

    assert await rig.child_names() == [b"report.pdf"], "replace made no second node"
    landed, etag = await _live_file(rig, b"report.pdf")
    assert landed == node_id, "the version went onto the node that already held the name"
    assert await _versions(rig, node_id) == 2
    assert info.size == len(b"replaced")
    assert etag > 1, "the head swap bumped the node's etag"


async def test_a_replace_of_a_name_nobody_holds_creates_the_file(rig: Rig) -> None:
    """There is nothing to adopt, so the commit takes the name outright."""
    session_id, refs = await rig.upload(b"fresh.bin", b"abcdefgh")
    operation = await rig.completion.complete(session_id, refs, conflict="replace")
    await rig.completion.promote(session_id, operation.id)

    assert await rig.child_names() == [b"fresh.bin"]
    node_id, _ = await _live_file(rig, b"fresh.bin")
    assert await _versions(rig, node_id) == 1


async def test_a_replace_agreed_against_a_stale_etag_is_refused(rig: Rig) -> None:
    """The precondition fences the adoption, not the head swap that follows it.

    The caller compared against the version it read; a writer that moved the
    node on between the agreement and the commit must cost the caller a
    refusal, never a silently clobbered version.
    """
    first, first_refs = await rig.upload(b"shared.pdf", b"original")
    await rig.completion.promote(first, (await rig.completion.complete(first, first_refs)).id)
    node_id, seen = await _live_file(rig, b"shared.pdf")

    session_id, refs = await rig.upload(b"shared.pdf", b"replaced")
    operation = await rig.completion.complete(session_id, refs, conflict="replace", if_match=seen)
    # Somebody else lands a version while these parts are in flight.
    await rig.session.execute(
        text("UPDATE file_nodes SET etag = etag + 1 WHERE id = :id"), {"id": node_id}
    )
    await rig.session.commit()

    with pytest.raises(PreconditionFailed):
        await rig.completion.promote(session_id, operation.id)

    assert await _versions(rig, node_id) == 1, "the refused commit landed no version"
    assert await rig.state(session_id) == "aborted"


async def test_a_replace_whose_etag_still_matches_lands(rig: Rig) -> None:
    """The asymmetric half: a fenced replace nobody raced is not refused."""
    first, first_refs = await rig.upload(b"fenced.pdf", b"original")
    await rig.completion.promote(first, (await rig.completion.complete(first, first_refs)).id)
    node_id, seen = await _live_file(rig, b"fenced.pdf")

    session_id, refs = await rig.upload(b"fenced.pdf", b"replaced")
    operation = await rig.completion.complete(session_id, refs, conflict="replace", if_match=seen)
    await rig.completion.promote(session_id, operation.id)

    assert await _versions(rig, node_id) == 2


# ---------------------------------------------------------------------------
# The promote finishes under the lease its session was admitted under
# ---------------------------------------------------------------------------

HOLDER = "instance-a"


async def _hold(rig: Rig, instance: str = HOLDER) -> Lease:
    """Take the folder's lease, as a mount does before it writes."""
    async with rig.repo.transaction():
        return await LeaseService(rig.repo, rig.ctx, rig.clock).acquire(
            rig.folder_id, instance_id=instance, machine_id="laptop"
        )


async def _upload_under(
    rig: Rig, name: bytes, payload: bytes, lease: LeaseContext
) -> tuple[SessionId, list[PartRef]]:
    """``Rig.upload``, but the session is opened carrying a lease context."""
    opened = await rig.uploads.open(
        rig.drive_id, rig.folder_id, name, declared_size=len(payload), lease=lease
    )
    session_id = SessionId(opened.id)
    refs: list[PartRef] = []
    for index in range(0, len(payload), SMALL_PART):
        chunk = payload[index : index + SMALL_PART]
        part_no = index // SMALL_PART + 1
        await rig.uploads.put_part(
            session_id, part_no, _stream(chunk), size=len(chunk), checksum=_digest(chunk)
        )
        refs.append(PartRef(part_no=part_no, size=len(chunk), checksum=_digest(chunk)))
    return session_id, refs


async def test_a_session_opened_under_a_lease_promotes_under_that_lease(rig: Rig) -> None:
    """The holder's own new file lands its node *and* its bytes.

    The commit runs later and elsewhere, with no headers of its own, so the
    only thing standing between the holder and their own fence is the epoch the
    session recorded when it was admitted. If the create half of the promote
    does not carry it, the node is never made and the holder's file is silently
    lost — which is what this pins.
    """
    lease = await _hold(rig)
    payload = b"held-bytes-payload"
    session_id, refs = await _upload_under(
        rig, b"held.txt", payload, held_by(rig.ctx, lease.epoch, HOLDER)
    )

    operation = await rig.completion.complete(session_id, refs)
    await rig.completion.promote(session_id, operation.id)

    settled = await rig.completion._ops.get(operation.id)
    assert settled.state == "done"
    assert settled.result_node_id is not None
    assert await _read(rig, NodeId(settled.result_node_id)) == payload
    assert await rig.child_names() == [b"held.txt"]


async def test_a_session_whose_lease_moved_on_is_refused_at_promote(rig: Rig) -> None:
    """The asymmetric half: an epoch that has since been superseded is fenced.

    The session was admitted, so it carries *an* epoch — but the folder has
    changed hands since, and the fence compares the recorded epoch against the
    live one rather than merely noticing that some epoch is present.
    """
    first = await _hold(rig)
    session_id, refs = await _upload_under(
        rig, b"stale.txt", b"stale-bytes", held_by(rig.ctx, first.epoch, HOLDER)
    )
    operation = await rig.completion.complete(session_id, refs)

    async with rig.repo.transaction():
        service = LeaseService(rig.repo, rig.ctx, rig.clock)
        await service.release(rig.folder_id, epoch=first.epoch, instance_id=HOLDER)
    second = await _hold(rig, instance="instance-b")
    assert second.epoch > first.epoch

    with pytest.raises(LeaseConflict):
        await rig.completion.promote(session_id, operation.id)

    assert await rig.child_names() == [], "the fenced commit left no node behind"


def _platform_ctx() -> ActingContext:
    """The context the Temporal worker promotes under.

    The shape ``worker.files_bootstrap.janitor_context`` builds: a SERVICE
    principal that authenticated nothing, holds no lease, and is a different
    principal from every human in the org. Spelled here rather than imported
    because api-core cannot see the worker package; what matters to the fence
    is only that it is not the holder.
    """
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.SERVICE, id="files-janitor", org_id=uuid.uuid4()
        )
    )


def _worker(rig: Rig) -> UploadCompletion:
    """The commit half as the worker builds it — the same library entry point
    ``worker.tasks.files.promote`` calls, on a context of its own."""
    return UploadCompletion(
        rig.repo,
        _platform_ctx(),
        rig.clock,
        _RootedDomainStore(rig.store, rig.domain_id),
        part_size=SMALL_PART,
    )


async def test_the_worker_promoting_is_not_the_holder_and_lands_the_file_anyway(
    rig: Rig,
) -> None:
    """The two halves of an upload run as two different principals.

    The open is fenced inside the holder's own request; the commit runs in the
    Temporal worker, which acts for the platform — so the caller at promote
    time is nobody's holder, and a fence that read the holder off the promoting
    context would refuse the holder their own file. What the fence compares is
    what the session recorded when it was admitted.
    """
    lease = await _hold(rig)
    payload = b"worker-promoted-payload"
    session_id, refs = await _upload_under(
        rig, b"worker.txt", payload, held_by(rig.ctx, lease.epoch, HOLDER)
    )

    operation = await rig.completion.complete(session_id, refs)
    await _worker(rig).promote(session_id, operation.id)

    settled = await rig.completion._ops.get(operation.id)
    assert settled.state == "done"
    assert settled.result_node_id is not None
    assert await _read(rig, NodeId(settled.result_node_id)) == payload
    assert await rig.child_names() == [b"worker.txt"]


async def test_the_worker_is_still_fenced_when_the_folder_changed_hands(rig: Rig) -> None:
    """The asymmetric twin: carrying the holder is not dropping the fence.

    Same worker, same session, but the folder was handed to a second machine
    while the parts were in flight. The recorded epoch is no longer the live
    one, so the commit is refused and the folder never grows the node — which
    is what a fence that merely stopped checking would fail to do.
    """
    first = await _hold(rig)
    session_id, refs = await _upload_under(
        rig, b"handed-on.txt", b"handed-on-bytes", held_by(rig.ctx, first.epoch, HOLDER)
    )
    operation = await rig.completion.complete(session_id, refs)

    async with rig.repo.transaction():
        await LeaseService(rig.repo, rig.ctx, rig.clock).release(
            rig.folder_id, epoch=first.epoch, instance_id=HOLDER
        )
    second = await _hold(rig, instance="instance-b")
    assert second.epoch > first.epoch

    with pytest.raises(LeaseConflict):
        await _worker(rig).promote(session_id, operation.id)

    assert await rig.child_names() == [], "the fenced commit left no node behind"

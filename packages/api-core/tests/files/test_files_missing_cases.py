"""The failure cases that had no test of their own.

Only the bullets the depended-on lanes do not already cover live here; where one
does, the existing test is named rather than copied:

``the API dies after store.put and before the session row is updated``
    `packages/api-core/tests/files/content/test_files_content_crash_points.py` kills a put at
    `content.after_store_put` and asserts the session still owns the object and
    the sweeper reclaims it — the create-before-reference proof.
``dedup decided per node, not per org``
    `packages/api-core/tests/files/content/test_files_content_dedup.py`.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from alkera_core.config import settings
from alkera_core.files import names
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.content import ContentService
from alkera_core.files.db_retry import RetryBudget, with_db_retries
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.ids import DomainId, NodeId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store import keys
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.models.files.versions import FileVersion
from alkera_test_support.files.faulty_store import FaultSchedule, FaultyStore
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files.content.conftest import content_ctx, stream

pytestmark = pytest.mark.asyncio


#: Bytes no dedup domain has seen, so "one object" is a claim about this run.
async def _no_wait(_seconds: float) -> None:
    """The retry ladder never sleeps in a test; the collision is already over."""
    return None


SHARED_PAYLOAD = b"identical bytes, two uploaders, " + b"z" * (settings.files_inline_max_bytes + 1)


@dataclass(frozen=True, slots=True)
class Uploader:
    """One writer: its own session, its own service, its own checkpoint gate."""

    service: ContentService
    node_id: NodeId
    checkpoints: PausingCheckpoints
    session: AsyncSession
    clock: FakeClock

    async def put(self, payload: bytes) -> None:
        """One upload, through the service boundary's retry ladder.

        Two writers publishing into one drive at once really do deadlock in
        Postgres (`40P01`) — the rollup rows up the shared ancestor chain are
        taken in whichever order each writer reached them. That
        verdict is retried at the service boundary with a bounded budget, so
        the upload goes through `with_db_retries`, and the deadlock stays
        invisible to the caller instead of surfacing as a failed upload.
        """

        async def once() -> None:
            await self.service.put_version(
                self.node_id, stream(payload), size_declared=len(payload), if_match=0
            )

        await with_db_retries(
            once, budget=RetryBudget(attempts=4), clock=self.clock, sleep=_no_wait
        )


@dataclass(frozen=True, slots=True)
class TwoUploaderRig:
    first: Uploader
    second: Uploader
    domain_root: Path
    reader: AsyncSession

    def objects(self) -> list[str]:
        """Every object the domain actually holds, read off the disk."""
        # `.as_posix()`: a store key is `/`-separated everywhere, so the disk
        # walk has to be spelled the same way the key is.
        return sorted(
            p.relative_to(self.domain_root).as_posix()
            for p in self.domain_root.rglob("*")
            if p.is_file()
        )

    async def head_hash(self, node_id: NodeId) -> tuple[str, int]:
        row = (
            await self.reader.execute(
                sa.select(FileVersion.content_hash, FileVersion.size_bytes)
                .where(FileVersion.node_id == uuid.UUID(str(node_id)))
                .order_by(FileVersion.seq.desc())
                .limit(1)
            )
        ).one()
        return str(row[0]), int(row[1])


@pytest.fixture
async def two_uploaders(
    files_session: AsyncSession,
    files_engine: AsyncEngine,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    tmp_path: Path,
) -> AsyncIterator[TwoUploaderRig]:
    """Two writers in one org, one dedup domain, one store root, two sessions.

    Both hold an upload open across the checkpoint the test releases them at, so
    the pool gives each its own connection for as long as the race lasts.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("papers/ papers/a.bin other/ other/b.bin", drive=drive)
    domain_id = DomainId(drive.dedup_domain_id)
    domain_root = tmp_path / "domains" / str(domain_id)

    made: list[Uploader] = []
    for node_name in ("papers/a.bin", "other/b.bin"):
        session = AsyncSession(bind=files_engine, expire_on_commit=False)
        gate = PausingCheckpoints()
        store = _RootedDomainStore(
            FaultyStore(FilesystemStore(domain_root, clock=clock.now), FaultSchedule(faults=())),
            domain_id,
        )
        repo = FilesRepo(session, OrgScope(org_team_id=files_org.org_team_id))
        made.append(
            Uploader(
                service=ContentService(repo, content_ctx(), clock, store, checkpoints=gate),
                node_id=NodeId(tree[node_name].id),
                checkpoints=gate,
                session=session,
                clock=clock,
            )
        )
    try:
        yield TwoUploaderRig(
            first=made[0], second=made[1], domain_root=domain_root, reader=files_session
        )
    finally:
        for uploader in made:
            await uploader.session.close()


# ---- concurrent identical uploads ---------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flaky: the 'content.after_head' checkpoint timed out on a Windows runner on a PR "
    "that could not reach it (Sep 30 2026); re-enable after the Windows timing is found",
)
@pytest.mark.parametrize(
    "release_order",
    [
        pytest.param(("first", "second"), id="first-publishes-first"),
        pytest.param(("second", "first"), id="second-publishes-first"),
    ],
)
async def test_two_uploads_of_identical_bytes_both_succeed_and_share_one_object(
    two_uploaders: TwoUploaderRig, release_order: tuple[str, str]
) -> None:
    """Both writers looked and saw nothing; whoever lands second is not an error.

    Both are held at `content.after_head` — the window where each has decided
    "the org does not hold these bytes yet" — and then released one at a time,
    in each order. The loser's publish collides with an object that appeared
    under it; that is success once `head` verifies the bytes, so
    neither call may raise and both nodes must end at the same content hash
    over a single stored object.
    """
    for uploader in (two_uploaders.first, two_uploaders.second):
        uploader.checkpoints.pause("content.after_head")

    tasks = {
        "first": asyncio.create_task(two_uploaders.first.put(SHARED_PAYLOAD)),
        "second": asyncio.create_task(two_uploaders.second.put(SHARED_PAYLOAD)),
    }
    for uploader in (two_uploaders.first, two_uploaders.second):
        await uploader.checkpoints.wait_paused("content.after_head")

    for who in release_order:
        getattr(two_uploaders, who).checkpoints.release("content.after_head")
        await tasks[who]

    expected = hash_bytes(SHARED_PAYLOAD)
    assert await two_uploaders.head_hash(two_uploaders.first.node_id) == (
        expected.content_hash.hex(),
        len(SHARED_PAYLOAD),
    )
    assert await two_uploaders.head_hash(two_uploaders.second.node_id) == (
        expected.content_hash.hex(),
        len(SHARED_PAYLOAD),
    )
    # One content object, and nothing left staged under `incoming/`.
    assert two_uploaders.objects() == [keys.object_key(expected.content_hash)]


# ---- empty files ---------------------------------------------------------


async def test_an_empty_file_gets_a_real_version_at_the_canonical_empty_hash(
    two_uploaders: TwoUploaderRig,
) -> None:
    """Size 0 is a file, not a missing one: inline, a real version, the empty hash."""
    await two_uploaders.first.put(b"")

    content_hash, size = await two_uploaders.head_hash(two_uploaders.first.node_id)
    assert (content_hash, size) == (hash_bytes(b"").content_hash.hex(), 0)
    # Inline, so the empty file costs the store nothing at all.
    assert two_uploaders.objects() == []


async def test_an_empty_file_and_a_one_byte_file_do_not_share_a_hash(
    two_uploaders: TwoUploaderRig,
) -> None:
    """The negative twin: the empty hash is the hash of nothing, not a sentinel."""
    await two_uploaders.first.put(b"")
    await two_uploaders.second.put(b"\x00")

    empty, _ = await two_uploaders.head_hash(two_uploaders.first.node_id)
    one, _ = await two_uploaders.head_hash(two_uploaders.second.node_id)
    assert empty != one


async def test_a_stream_longer_than_a_declared_empty_file_is_refused(
    two_uploaders: TwoUploaderRig,
) -> None:
    """Zero declared bytes means zero: a body under it is a size mismatch."""
    from alkera_core.files.errors import InvalidRequest

    with pytest.raises(InvalidRequest) as raised:
        await two_uploaders.first.service.put_version(
            two_uploaders.first.node_id, stream(b"x"), size_declared=0, if_match=0
        )
    assert raised.value.code == "files.size_mismatch"
    assert two_uploaders.objects() == []


# ---- Linux-legal everything ---------------------------------------------


@pytest.mark.parametrize(
    ("name", "windows_safe", "display_warning"),
    [
        pytest.param(b"\xff" * names.NAME_MAX_BYTES, True, True, id="undecodable-at-the-ceiling"),
        pytest.param("naïve\u200b.txt".encode(), True, True, id="zero-width-space"),
        pytest.param(b"COM1", False, False, id="windows-device"),
        pytest.param(b"COM10", True, False, id="windows-device-negative-twin"),
        pytest.param("ふつうの名前.txt".encode(), True, False, id="ordinary-non-ascii"),
        pytest.param(b"trailing.", False, False, id="trailing-dot"),
    ],
)
async def test_a_name_only_a_client_objects_to_is_flagged_never_refused(
    name: bytes, windows_safe: bool, display_warning: bool
) -> None:
    """One drive is shared by machines that disagree about what a name may be.

    A Windows device stem, a trailing dot, a zero-width space and a run of
    bytes no decoder accepts are all ordinary names on the box that made
    them. (A bidirectional control is the exception: it makes a name read as
    another, and is refused.) Refusing them would cost that person a file they can see, so the
    drive stores them and hands the client a flag to decide with instead.
    """
    names.validate(name)  # never raises for a name the drive takes
    flagged = names.flags(name)
    assert (flagged.windows_safe, flagged.display_warning) == (windows_safe, display_warning)
    # The flag is advice, not a refusal: the name round-trips through display.
    assert names.parse_display(names.display(name)) == name


@pytest.mark.parametrize(
    ("name", "code"),
    [
        pytest.param(b"\xff" * 255, "too_long", id="undecodable-past-the-ceiling"),
        pytest.param(bytes(range(1, 32)), "control", id="control-bytes"),
        pytest.param(b" leading", "surrounding_space", id="leading-space"),
    ],
)
async def test_a_name_no_machine_could_hold_is_refused_and_still_describable(
    name: bytes, code: str
) -> None:
    """Where the two tables meet: the drive refuses it, and still reads it back.

    A row stored under an older, looser rule is never migrated or renamed, so a
    listing that could not describe one would be a file its owner can no longer
    see. Only the path that PROPOSES a name judges it.
    """
    with pytest.raises(names.InvalidName) as raised:
        names.validate(name)
    assert raised.value.code == code
    names.flags(name)
    assert names.parse_display(names.display(name)) == name

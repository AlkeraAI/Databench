"""The drive settles a two-way write itself, and never loses the displaced bytes.

A chat folder a machine holds takes writes from both sides. These drive the
real write services — ``ContentService.put_version`` for both sides, the upload
session pair for the holder's conflict submission — against a real lease, real
Postgres and a real filesystem store, and assert on what the drive then holds:
which bytes kept the name, where the displaced bytes went, the conflict row,
the history rows and the announcements.

Every case fails without its mechanism: take the holder exemption out of the
content commit and the first case is a 412; drop the copy and the name, the
count and the carried store key are gone; drop the machine-managed test and a
copy appears under ``.git``; drop the ceiling and a sixth copy is minted. The
property at the bottom interleaves all of it at random and checks the one
promise the feature makes: every byte sequence written is still in the drive.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.config import settings
from alkera_core.files import conflict_resolution
from alkera_core.files.clock import FakeClock
from alkera_core.files.conflicts import conflicted_copy_name
from alkera_core.files.content import ContentService
from alkera_core.files.errors import Conflict, PreconditionFailed
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.ids import DomainId, DriveId, NodeId, OrgScope, SessionId
from alkera_core.files.leases import LeaseContext, LeaseService
from alkera_core.files.repo import FilesRepo
from alkera_core.files.retention import HEAD_ONLY, apply_label, prune_versions
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.files.uploads import PartRef, UploadCompletion, UploadService
from alkera_core.models.event_outbox import EventOutbox
from alkera_core.models.files.history import FileConflict, FileHistory
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from alkera_test_support.files.faulty_store import FaultSchedule, FaultyStore
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.fencing import held_by

pytestmark = pytest.mark.asyncio

INSTANCE = "box-1"
MACHINE = "alkera-demo-box"
#: Past the inline cap, so the bytes are a store object and "no bytes moved"
#: is a count of objects on disk rather than a column.
BIG = settings.files_inline_max_bytes + 4096
#: The admin the factory seeds is named after its role.
ADMIN_NAME = "Admin Tester"
FILES = (
    "chat/ chat/scratch/ chat/scratch/report.md chat/scratch/notes.txt "
    "chat/scratch/.git/ chat/scratch/.git/index chat/scratch/node_modules/ "
    "chat/scratch/node_modules/pkg.json chat/scratch/logs/ chat/scratch/logs/run.log"
)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _bytes(tag: str, size: int = 64) -> bytes:
    seed = hashlib.sha256(tag.encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


def _hash(payload: bytes) -> str:
    return hash_bytes(payload).content_hash.hex()


async def _stream(payload: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(payload), 4096):
        yield payload[start : start + 4096]
    if not payload:
        return


@dataclass
class Chat:
    """A leased, awake chat folder and the two writers that share it."""

    session: AsyncSession
    repo: FilesRepo
    ctx: ActingContext
    clock: FakeClock
    store: FaultyStore
    domain_root: Path
    domain_id: DomainId
    drive_id: DriveId
    ids: dict[str, uuid.UUID]
    epoch: int
    org: FilesOrg
    #: The etag the holder last agreed with the drive, per file.
    agreed: dict[str, int] = field(default_factory=dict)

    @property
    def scoped(self) -> _RootedDomainStore:
        return _RootedDomainStore(self.store, self.domain_id)

    @property
    def fence(self) -> LeaseContext:
        return held_by(self.ctx, self.epoch, INSTANCE)

    def content(self) -> ContentService:
        return ContentService(self.repo, self.ctx, self.clock, self.scoped)

    def node_id(self, name: str) -> NodeId:
        return NodeId(self.ids[name])

    async def etag(self, name: str) -> int:
        return int(await self._column(name, "etag"))

    async def _column(self, name: str, column: str) -> Any:
        return (
            await self.session.execute(
                text(f"SELECT {column} FROM file_nodes WHERE id = :node"),
                {"node": self.ids[name]},
            )
        ).scalar_one()

    async def web(self, name: str, payload: bytes) -> None:
        """A person saving the file in the browser: current base, no fence."""
        await self.content().put_version(
            self.node_id(name),
            _stream(payload),
            size_declared=len(payload),
            if_match=await self.etag(name),
            lease=None,
        )

    async def holder(self, name: str, payload: bytes, *, base: int | None = None) -> None:
        """The machine landing its disk's bytes, on the base it last agreed."""
        agreed = self.agreed.get(name, 0) if base is None else base
        await self.content().put_version(
            self.node_id(name),
            _stream(payload),
            size_declared=len(payload),
            if_match=agreed,
            lease=self.fence,
        )
        self.agreed[name] = await self.etag(name)

    async def head_hash(self, node_id: uuid.UUID) -> str | None:
        return (
            await self.session.execute(
                text(
                    "SELECT v.content_hash FROM file_nodes n "
                    "JOIN file_versions v ON v.id = n.head_version_id WHERE n.id = :node"
                ),
                {"node": node_id},
            )
        ).scalar_one_or_none()

    async def version_hashes(self, node_id: uuid.UUID) -> list[str]:
        rows = await self.session.execute(
            select(FileVersion.content_hash)
            .where(FileVersion.node_id == node_id)
            .order_by(FileVersion.seq)
        )
        return list(rows.scalars().all())

    async def conflicts(self, name: str) -> list[FileConflict]:
        self.session.expire_all()
        rows = await self.session.execute(
            select(FileConflict)
            .where(FileConflict.node_id == self.ids[name])
            .order_by(FileConflict.created_at, FileConflict.id)
        )
        return list(rows.scalars().all())

    async def siblings(self, name: str) -> dict[bytes, uuid.UUID]:
        rows = await self.session.execute(
            text(
                "SELECT name, id FROM file_nodes WHERE parent_id = "
                "(SELECT parent_id FROM file_nodes WHERE id = :node) AND trashed_at IS NULL"
            ),
            {"node": self.ids[name]},
        )
        return {bytes(row.name): row.id for row in rows}

    def objects(self) -> int:
        """How many objects the store holds under this drive's domain."""
        return sum(1 for path in self.domain_root.rglob("*") if path.is_file())


@pytest.fixture
async def chat(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    tmp_path: Path,
) -> Chat:
    drive = await files_factory.drive()
    nodes = await files_factory.tree(FILES, drive=drive)
    await files_session.execute(
        text("UPDATE file_nodes SET subtype = 'chat', target_object_id = :object WHERE id = :node"),
        {"object": uuid.uuid4(), "node": nodes["chat"].id},
    )
    await files_session.commit()
    repo = FilesRepo(files_session, OrgScope(org_team_id=files_org.org_team_id))
    ctx = _ctx(files_org)
    async with repo.transaction():
        grant = await LeaseService(repo, ctx, clock).acquire(
            NodeId(nodes["chat"].id), instance_id=INSTANCE, machine_id=MACHINE, purpose="chat"
        )
    await files_session.execute(
        text("UPDATE file_leases SET accepts_inbound = true WHERE node_id = :node"),
        {"node": nodes["chat"].id},
    )
    await files_session.commit()
    domain_id = DomainId(drive.dedup_domain_id)
    domain_root = tmp_path / "domains" / str(domain_id)
    store = FaultyStore(FilesystemStore(domain_root, clock=clock.now), FaultSchedule(faults=()))
    return Chat(
        session=files_session,
        repo=repo,
        ctx=ctx,
        clock=clock,
        store=store,
        domain_root=domain_root,
        domain_id=domain_id,
        drive_id=DriveId(drive.id),
        ids={
            path.rsplit("/", 1)[-1]: node.id for path, node in nodes.items() if node.kind == "file"
        },
        epoch=grant.epoch,
        org=files_org,
    )


# -- the holder's upload over a head the web moved ----------------------------


async def test_the_holders_upload_keeps_the_name_and_the_webs_bytes_become_a_copy(
    chat: Chat,
) -> None:
    """No 412: the holder's bytes are the head, and the web's are a copy node
    whose version points at the SAME stored object — no byte moved."""
    web, box = _bytes("web", BIG), _bytes("box", BIG)
    await chat.holder("report.md", _bytes("first", BIG))
    await chat.web("report.md", web)
    objects_before = chat.objects()

    await chat.holder("report.md", box)

    node_id = chat.ids["report.md"]
    assert await chat.head_hash(node_id) == _hash(box)
    [row] = await chat.conflicts("report.md")
    assert row.state == "auto"
    assert row.arrived_from == "holder"
    assert row.who == MACHINE, "who names the writer whose bytes kept the name"
    assert row.displaced_by == ADMIN_NAME
    assert row.copy_node_id is not None
    copy = await chat.session.get(FileNode, row.copy_node_id)
    assert copy is not None
    assert copy.name == conflicted_copy_name(b"report.md", ADMIN_NAME, chat.clock.now())
    assert copy.parent_id == (await chat.session.get(FileNode, node_id)).parent_id  # type: ignore[union-attr]
    assert copy.node_metadata.get("conflict_of") == str(node_id)
    displaced = await chat.session.get(FileVersion, row.theirs_version_id)
    carried = await chat.session.get(FileVersion, copy.head_version_id)
    assert displaced is not None and carried is not None
    assert displaced.node_id == node_id, "the displaced bytes stay a version of the node"
    assert row.base_version_id == displaced.id
    assert carried.content_hash == displaced.content_hash == _hash(web)
    assert carried.store_key == displaced.store_key
    # One new object — the holder's bytes. The copy took none.
    assert chat.objects() == objects_before + 1


@pytest.mark.parametrize(
    ("writer", "code"),
    [
        pytest.param("web", PreconditionFailed, id="a-person-on-a-stale-base-is-still-412"),
        pytest.param("stranger-epoch", Conflict, id="a-superseded-epoch-is-still-fenced"),
    ],
)
async def test_only_the_fenced_holder_is_let_past_a_stale_base(
    chat: Chat, writer: str, code: type[Exception]
) -> None:
    await chat.web("report.md", _bytes("one"))
    stale = await chat.etag("report.md") - 1
    lease = None if writer == "web" else held_by(chat.ctx, chat.epoch + 1, INSTANCE)

    with pytest.raises(code):
        await chat.content().put_version(
            chat.node_id("report.md"),
            _stream(_bytes("late")),
            size_declared=64,
            if_match=stale,
            lease=lease,
        )

    assert await chat.head_hash(chat.ids["report.md"]) == _hash(_bytes("one"))
    assert await chat.conflicts("report.md") == []


async def test_a_holder_on_the_current_base_or_with_the_heads_bytes_makes_no_conflict(
    chat: Chat,
) -> None:
    """The negative twins: nothing displaced, nothing settled."""
    await chat.holder("report.md", _bytes("a"))
    await chat.holder("report.md", _bytes("b"))
    await chat.web("report.md", _bytes("c"))
    # Stale base, but the disk already holds what the web wrote.
    await chat.holder("report.md", _bytes("c"))

    assert await chat.conflicts("report.md") == []
    assert set(await chat.siblings("report.md")) == {
        b"report.md",
        b"notes.txt",
        b".git",
        b"node_modules",
        b"logs",
    }


async def test_a_second_copy_in_the_same_minute_is_numbered_two(chat: Chat) -> None:
    for round_ in range(2):
        await chat.web("report.md", _bytes(f"web-{round_}"))
        await chat.holder("report.md", _bytes(f"box-{round_}"))

    first = conflicted_copy_name(b"report.md", ADMIN_NAME, chat.clock.now())
    second = first.replace(b").md", b") (2).md")
    names = await chat.siblings("report.md")
    assert first in names and second in names
    assert await chat.head_hash(names[first]) == _hash(_bytes("web-0"))
    assert await chat.head_hash(names[second]) == _hash(_bytes("web-1"))


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("index", id="under-dot-git"),
        pytest.param("pkg.json", id="under-node-modules"),
        pytest.param("run.log", id="under-a-head-only-label"),
    ],
)
async def test_a_machine_managed_path_keeps_the_displaced_bytes_as_a_version_only(
    chat: Chat, name: str
) -> None:
    if name == "run.log":
        async with chat.repo.transaction():
            folder = await chat.repo.node(
                NodeId((await chat.session.get(FileNode, chat.ids[name])).parent_id)  # type: ignore[arg-type,union-attr]
            )
            assert folder is not None
            await apply_label(chat.repo, chat.ctx, folder, HEAD_ONLY)
    before = set(await chat.siblings(name))
    await chat.web(name, _bytes("web"))

    await chat.holder(name, _bytes("box"))

    assert set(await chat.siblings(name)) == before, "no copy is minted on a machine's path"
    [row] = await chat.conflicts(name)
    assert row.copy_node_id is None
    assert row.state == "auto"
    assert _hash(_bytes("web")) in await chat.version_hashes(chat.ids[name])
    assert await chat.head_hash(chat.ids[name]) == _hash(_bytes("box"))
    history = await _history(chat, chat.ids[name], "conflict")
    assert [entry.after["copy"] for entry in history] == [None]  # type: ignore[index]


async def test_head_only_pruning_keeps_the_displaced_version_and_finishes_its_pass(
    chat: Chat,
) -> None:
    """Under a ``head-only`` label the pruner keeps nothing but the head, yet a
    versions-only resolution's displaced bytes live nowhere else: the conflict
    row names that version, so it stays in history, and the pass that meets it
    still prunes what no rule keeps instead of dying on the reference."""
    name = "run.log"
    async with chat.repo.transaction():
        folder = await chat.repo.node(
            NodeId((await chat.session.get(FileNode, chat.ids[name])).parent_id)  # type: ignore[arg-type,union-attr]
        )
        assert folder is not None
        await apply_label(chat.repo, chat.ctx, folder, HEAD_ONLY)
    await chat.holder(name, _bytes("first"))
    await chat.holder(name, _bytes("second"))
    await chat.web(name, _bytes("web"))
    await chat.holder(name, _bytes("box"))
    [row] = await chat.conflicts(name)

    async with chat.repo.transaction():
        pruned = await prune_versions(chat.repo, now=chat.clock.now() + timedelta(days=400))

    kept = await chat.version_hashes(chat.ids[name])
    assert _hash(_bytes("web")) in kept, "the displaced bytes are still a version"
    assert await chat.head_hash(chat.ids[name]) == _hash(_bytes("box"))
    assert _hash(_bytes("second")) not in kept, "what nothing names is still pruned"
    assert pruned >= 1
    assert await chat.session.get(FileVersion, row.theirs_version_id) is not None


async def test_past_the_ceiling_a_displacement_is_a_new_version_of_the_newest_copy(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "files_conflict_copies_max", 2)
    for round_ in range(3):
        chat.clock.advance(timedelta(minutes=1))
        await chat.web("report.md", _bytes(f"web-{round_}"))
        await chat.holder("report.md", _bytes(f"box-{round_}"))

    rows = await chat.conflicts("report.md")
    copies = [row.copy_node_id for row in rows]
    assert len(set(copies)) == 2, "the ceiling holds the number of copy nodes"
    assert copies[2] == copies[1], "the third displacement went to the newest copy"
    newest = copies[1]
    assert newest is not None
    assert await chat.head_hash(newest) == _hash(_bytes("web-2"))
    assert await chat.version_hashes(newest) == [_hash(_bytes("web-1")), _hash(_bytes("web-2"))]


async def test_the_settlement_is_on_record_and_announced_for_both_rows(chat: Chat) -> None:
    await chat.web("report.md", _bytes("web"))
    await chat.holder("report.md", _bytes("box"))
    [row] = await chat.conflicts("report.md")
    copy_id = row.copy_node_id
    assert copy_id is not None
    expected = {
        "conflict_id": str(row.id),
        "kept": str(row.mine_version_id),
        "displaced": str(row.theirs_version_id),
        "copy": str(copy_id),
        "arrived_from": "holder",
        "who": MACHINE,
        "displaced_by": ADMIN_NAME,
    }

    for node_id in (chat.ids["report.md"], copy_id):
        [entry] = await _history(chat, node_id, "conflict")
        assert entry.after == expected
        reasons = await _announced(chat, node_id)
        assert "conflict" in reasons
    # The machine never saw the web's bytes: it is asked to fetch the copy.
    owed = (
        await chat.session.execute(
            text("SELECT state FROM file_lease_live_entries WHERE node_id = :node"),
            {"node": copy_id},
        )
    ).scalar_one_or_none()
    assert owed == "inbound"


# -- the holder's live upload, in the shape the live sync sends it -----------


@pytest.mark.parametrize(
    "size",
    [
        pytest.param(96, id="inline-bytes"),
        pytest.param(BIG, id="stored-object"),
    ],
)
async def test_a_live_sync_session_on_a_stale_base_settles_rather_than_overwrites(
    chat: Chat, size: int
) -> None:
    """The holder's live upload is a fenced session -- open, one part, complete
    with ``replace`` and ``If-Match`` the etag the holder agreed its disk's
    bytes at -- promoted later. When the web moved the head in between (the
    walkthrough's browser Replace), the holder's bytes keep the name and the
    web's become a copy, with the conflict row, the history and both events."""
    await chat.holder("notes.txt", _bytes("agreed", size))
    agreed = chat.agreed["notes.txt"]
    web = _bytes("web-replace", size)
    await chat.web("notes.txt", web)
    assert await chat.etag("notes.txt") != agreed
    box = _bytes("box-append", size)

    uploads = UploadService(chat.repo, chat.ctx, chat.clock, chat.scoped)
    completion = UploadCompletion(chat.repo, chat.ctx, chat.clock, chat.scoped)
    node = await chat.session.get(FileNode, chat.ids["notes.txt"])
    assert node is not None and node.parent_id is not None
    opened = await uploads.open(
        chat.drive_id,
        NodeId(node.parent_id),
        b"notes.txt",
        declared_size=len(box),
        lease=chat.fence,
    )
    digest = hash_bytes(box).content_hash
    await uploads.put_part(SessionId(opened.id), 1, _stream(box), size=len(box), checksum=digest)
    operation = await completion.complete(
        SessionId(opened.id),
        [PartRef(part_no=1, size=len(box), checksum=digest)],
        conflict="replace",
        if_match=agreed,
    )
    await completion.promote(SessionId(opened.id), operation.id)

    node_id = chat.ids["notes.txt"]
    assert await chat.head_hash(node_id) == _hash(box), "the last arrival keeps the name"
    [row] = await chat.conflicts("notes.txt")
    assert (row.state, row.arrived_from, row.who, row.displaced_by) == (
        "auto",
        "holder",
        MACHINE,
        ADMIN_NAME,
    )
    assert row.copy_node_id is not None
    copy = await chat.session.get(FileNode, row.copy_node_id)
    assert copy is not None
    assert copy.name == conflicted_copy_name(b"notes.txt", ADMIN_NAME, chat.clock.now())
    copy_id = row.copy_node_id
    assert await chat.head_hash(copy_id) == _hash(web), "the web's bytes are a head"
    for announced in (node_id, copy_id):
        assert [entry.after["copy"] for entry in await _history(chat, announced, "conflict")] == [
            str(copy_id)
        ]
        assert "conflict" in await _announced(chat, announced)


# -- the holder's conflict submission -----------------------------------------


async def _submit(
    chat: Chat, payload: bytes, *, conflict_of: str | None, lease: LeaseContext | None
) -> tuple[Any, UploadCompletion, SessionId, list[PartRef]]:
    uploads = UploadService(chat.repo, chat.ctx, chat.clock, chat.scoped)
    completion = UploadCompletion(chat.repo, chat.ctx, chat.clock, chat.scoped)
    beside = await chat.session.get(FileNode, chat.ids[conflict_of or "report.md"])
    assert beside is not None and beside.parent_id is not None
    opened = await uploads.open(
        chat.drive_id,
        NodeId(beside.parent_id),
        bytes(beside.name),
        declared_size=len(payload),
        lease=lease,
        conflict_of=None if conflict_of is None else chat.node_id(conflict_of),
        conflict_copy=True,
    )
    digest = hash_bytes(payload).content_hash
    await uploads.put_part(
        SessionId(opened.id), 1, _stream(payload), size=len(payload), checksum=digest
    )
    return (
        opened,
        completion,
        SessionId(opened.id),
        [PartRef(part_no=1, size=len(payload), checksum=digest)],
    )


async def test_a_conflict_submission_lands_beside_the_node_it_displaced(chat: Chat) -> None:
    """The drive names the copy at ``complete``, the node keeps its head and etag,
    and the submitted bytes are a version of the node as well as the copy's head."""
    await chat.web("report.md", _bytes("web"))
    etag = await chat.etag("report.md")
    payload = _bytes("box-displaced")
    _opened, completion, session_id, parts = await _submit(
        chat, payload, conflict_of="report.md", lease=chat.fence
    )

    operation = await completion.complete(session_id, parts)
    answer = await completion.submission(operation.id)
    assert answer is not None
    assert answer.name == conflicted_copy_name(b"report.md", MACHINE, chat.clock.now())
    await completion.promote(session_id, operation.id)

    assert answer.node_id is not None and answer.conflict_id is not None
    assert await chat.head_hash(answer.node_id) == _hash(payload)
    assert await chat.head_hash(chat.ids["report.md"]) == _hash(_bytes("web"))
    assert await chat.etag("report.md") == etag, "the original is untouched"
    assert _hash(payload) in await chat.version_hashes(chat.ids["report.md"])
    [row] = await chat.conflicts("report.md")
    assert row.id == answer.conflict_id
    assert (row.state, row.arrived_from, row.who, row.displaced_by) == (
        "auto",
        "web",
        ADMIN_NAME,
        MACHINE,
    )
    assert row.copy_node_id == answer.node_id


async def test_a_submission_naming_no_node_is_an_ordinary_file_the_drive_names(
    chat: Chat,
) -> None:
    payload = _bytes("orphan")
    _opened, completion, session_id, parts = await _submit(
        chat, payload, conflict_of=None, lease=chat.fence
    )
    operation = await completion.complete(session_id, parts)
    answer = await completion.submission(operation.id)
    await completion.promote(session_id, operation.id)

    assert answer is not None and answer.conflict_id is None and answer.node_id is not None
    assert answer.name == conflicted_copy_name(b"report.md", MACHINE, chat.clock.now())
    assert await chat.head_hash(answer.node_id) == _hash(payload)


@pytest.mark.parametrize(
    "who",
    [
        pytest.param("no-fence", id="a-writer-with-no-epoch"),
        pytest.param("stale-epoch", id="a-superseded-holder"),
        pytest.param("other-instance", id="another-instance-under-the-epoch"),
    ],
)
async def test_only_the_fenced_holder_may_open_a_conflict_submission(chat: Chat, who: str) -> None:
    lease = {
        "no-fence": None,
        "stale-epoch": held_by(chat.ctx, chat.epoch - 1, INSTANCE),
        "other-instance": held_by(chat.ctx, chat.epoch, "box-2"),
    }[who]
    with pytest.raises(Conflict) as refused:
        await _submit(chat, _bytes("x"), conflict_of="report.md", lease=lease)
    assert refused.value.code == "files.lease_mismatch"
    count = (
        await chat.session.execute(
            text(
                "SELECT count(*) FROM file_upload_sessions "
                "WHERE conflict_copy AND drive_id = :drive"
            ),
            {"drive": chat.drive_id},
        )
    ).scalar_one()
    assert count == 0


# -- confirming an automatic settlement ---------------------------------------


@pytest.mark.parametrize(
    ("keep", "head", "copy_trashed"),
    [
        pytest.param("mine", "box", True, id="keep-this-trashes-the-copy"),
        pytest.param("theirs", "web", True, id="keep-the-other-promotes-it-and-trashes-the-copy"),
        pytest.param("both", "box", False, id="keep-both-moves-nothing"),
    ],
)
async def test_each_keep_does_what_the_pane_says(
    chat: Chat, keep: str, head: str, copy_trashed: bool
) -> None:
    await chat.web("report.md", _bytes("web"))
    await chat.holder("report.md", _bytes("box"))
    [row] = await chat.conflicts("report.md")

    async with chat.repo.transaction():
        done = await conflict_resolution.resolve_conflict(
            chat.repo,
            chat.ctx,
            row.id,
            choice=keep,
            if_match=None,
            clock=chat.clock,  # type: ignore[arg-type]
        )

    assert done.state == "resolved"
    assert await chat.head_hash(chat.ids["report.md"]) == _hash(_bytes(head))
    copy = await chat.session.get(FileNode, row.copy_node_id)
    await chat.session.refresh(copy)
    assert copy is not None
    assert (copy.trashed_at is not None) is copy_trashed
    [closed] = await chat.conflicts("report.md")
    assert closed.state == "resolved" and closed.resolved_by == chat.org.admin_id
    # The box's disk holds its own bytes; only keeping the other changes that.
    owed = (
        await chat.session.execute(
            text("SELECT state FROM file_lease_live_entries WHERE node_id = :node"),
            {"node": chat.ids["report.md"]},
        )
    ).scalar_one_or_none()
    assert owed == ("inbound" if keep == "theirs" else None)
    async with chat.repo.transaction():
        assert await conflict_resolution.list_open_conflicts(chat.repo, chat.drive_id) == []


async def test_an_auto_conflict_is_listed_with_who_and_where(chat: Chat) -> None:
    await chat.web("report.md", _bytes("web"))
    await chat.holder("report.md", _bytes("box"))
    async with chat.repo.transaction():
        [listed] = await conflict_resolution.list_open_conflicts(chat.repo, chat.drive_id)
    assert (listed.state, listed.arrived_from, listed.who, listed.displaced_by) == (
        "auto",
        "holder",
        MACHINE,
        ADMIN_NAME,
    )
    assert listed.copy_node_id is not None


# -- a person's Replace upload under the lease ---------------------------------


async def _web_replace(chat: Chat, name: str, payload: bytes) -> None:
    """The browser's Replace: an upload session under the name, committed with
    ``replace`` against the etag the person saw, by someone who holds no lease."""
    uploads = UploadService(chat.repo, chat.ctx, chat.clock, chat.scoped)
    completion = UploadCompletion(chat.repo, chat.ctx, chat.clock, chat.scoped)
    existing = await chat.session.get(FileNode, chat.ids[name])
    assert existing is not None and existing.parent_id is not None
    seen = await chat.etag(name)
    opened = await uploads.open(
        chat.drive_id, NodeId(existing.parent_id), bytes(existing.name), declared_size=len(payload)
    )
    digest = hash_bytes(payload).content_hash
    await uploads.put_part(
        SessionId(opened.id), 1, _stream(payload), size=len(payload), checksum=digest
    )
    parts = [PartRef(part_no=1, size=len(payload), checksum=digest)]
    operation = await completion.complete(
        SessionId(opened.id), parts, conflict="replace", if_match=seen
    )
    await completion.promote(SessionId(opened.id), operation.id)


async def _trashed_beside(chat: Chat, name: str) -> list[uuid.UUID]:
    rows = await chat.session.execute(
        text(
            "SELECT id FROM file_nodes WHERE parent_id = "
            "(SELECT parent_id FROM file_nodes WHERE id = :node) AND trashed_at IS NOT NULL"
        ),
        {"node": chat.ids[name]},
    )
    return list(rows.scalars().all())


async def test_a_web_replace_under_the_lease_is_a_new_version_of_the_same_node(
    chat: Chat,
) -> None:
    """Replace keeps the file: the name still holds the node it held, with one
    more version whose bytes are the upload's, and nothing is trashed."""
    await chat.holder("report.md", _bytes("agent-v1"))
    await chat.holder("report.md", _bytes("agent-v2"))
    before = await chat.version_hashes(chat.ids["report.md"])
    upload = _bytes("person's upload")

    await _web_replace(chat, "report.md", upload)

    assert (await chat.siblings("report.md"))[b"report.md"] == chat.ids["report.md"]
    assert await chat.version_hashes(chat.ids["report.md"]) == [*before, _hash(upload)]
    assert await chat.head_hash(chat.ids["report.md"]) == _hash(upload)
    assert await _trashed_beside(chat, "report.md") == []


async def test_a_web_replace_racing_a_holder_write_keeps_the_node_and_files_the_box_s_copy(
    chat: Chat,
) -> None:
    """The holder was writing report.md when the person's Replace landed. Once
    the box has taken the Replace and handed back what it displaced, the name
    still holds the original node with the upload at its head and its history
    intact, the box's bytes are the copy beside it, and nothing is trashed."""
    await chat.holder("report.md", _bytes("agent-v1"))
    before = await chat.version_hashes(chat.ids["report.md"])
    upload, box = _bytes("person's upload"), _bytes("agent's concurrent append")

    await _web_replace(chat, "report.md", upload)
    _opened, completion, session_id, parts = await _submit(
        chat, box, conflict_of="report.md", lease=chat.fence
    )
    operation = await completion.complete(session_id, parts)
    answer = await completion.submission(operation.id)
    await completion.promote(session_id, operation.id)

    siblings = await chat.siblings("report.md")
    assert siblings[b"report.md"] == chat.ids["report.md"]
    assert await chat.head_hash(chat.ids["report.md"]) == _hash(upload)
    history = await chat.version_hashes(chat.ids["report.md"])
    assert history[: len(before) + 1] == [*before, _hash(upload)]
    assert answer is not None and answer.node_id is not None
    assert siblings[conflicted_copy_name(b"report.md", MACHINE, chat.clock.now())] == answer.node_id
    assert await chat.head_hash(answer.node_id) == _hash(box)
    [row] = await chat.conflicts("report.md")
    assert row.copy_node_id == answer.node_id
    assert await _trashed_beside(chat, "report.md") == []


# -- the property -------------------------------------------------------------


OPS = ("web", "holder_current", "holder_stale", "submit")


@pytest.mark.parametrize("seed", [pytest.param(seed, id=f"seed-{seed}") for seed in range(8)])
async def test_every_byte_sequence_ever_written_is_still_in_the_drive(
    chat: Chat, seed: int
) -> None:
    """Randomized interleavings of the holder's uploads — on its agreed base and
    on a stale one — its conflict submissions, and people's writes, on one
    node set. Afterwards every content hash that was ever the content of a
    file is the head of that node, a version in its history, or the head of a
    copy; and no holder write was ever refused for its base."""
    rng = random.Random(seed)
    names = ("report.md", "notes.txt", "index")
    written: dict[str, set[str]] = {name: set() for name in names}
    for step in range(24):
        name = rng.choice(names)
        op = rng.choice(OPS)
        payload = _bytes(f"{seed}-{step}-{op}", rng.choice((40, 96, BIG)))
        if rng.random() < 0.3:
            chat.clock.advance(timedelta(seconds=rng.randint(1, 90)))
        if op == "web":
            await chat.web(name, payload)
        elif op == "holder_current":
            await chat.holder(name, payload, base=await chat.etag(name))
        elif op == "holder_stale":
            await chat.holder(name, payload)
        else:
            # The holder applied the web's write over its disk and hands back
            # what that write displaced; its agreed base is the head now.
            _opened, completion, session_id, parts = await _submit(
                chat, payload, conflict_of=name, lease=chat.fence
            )
            operation = await completion.complete(session_id, parts)
            await completion.promote(session_id, operation.id)
            chat.agreed[name] = await chat.etag(name)
        written[name].add(_hash(payload))

    for name in names:
        kept = set(await chat.version_hashes(chat.ids[name]))
        copies: set[str] = set()
        for row in await chat.conflicts(name):
            if row.copy_node_id is not None:
                copies.update(await chat.version_hashes(row.copy_node_id))
                assert (
                    await chat.session.get(FileVersion, row.theirs_version_id)
                ).content_hash in await chat.version_hashes(row.copy_node_id)  # type: ignore[union-attr]
        head = await chat.head_hash(chat.ids[name])
        lost = written[name] - kept - copies - {head}
        assert not lost, f"{name}: bytes written but no longer in the drive: {sorted(lost)}"


# -- readers ------------------------------------------------------------------


async def _history(chat: Chat, node_id: uuid.UUID, kind: str) -> list[FileHistory]:
    chat.session.expire_all()
    rows = await chat.session.execute(
        select(FileHistory)
        .where(FileHistory.node_id == node_id, FileHistory.kind == kind)
        .order_by(FileHistory.seq)
    )
    return list(rows.scalars().all())


async def _announced(chat: Chat, node_id: uuid.UUID) -> list[str | None]:
    rows = await chat.session.execute(
        select(EventOutbox.payload).where(
            EventOutbox.type == "file_node.changed", EventOutbox.entity_id == str(node_id)
        )
    )
    return [payload.get("reason") for payload in rows.scalars().all()]

"""What a janitor test needs: an admin store, an age source and seeded roots."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DomainId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.models.files.history import FileContentGrant, FileTrashOp
from alkera_core.models.files.platform import FileSweepShard
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.uploads import FileUploadSession
from alkera_core.models.files.versions import FileVersion
from sqlalchemy.ext.asyncio import AsyncSession


def _load_concurrency_kit() -> Any:
    """The two-session kit, borrowed rather than reimplemented.

    The test folders have no packages, so a sibling conftest cannot be imported by
    name; loading it by path is what lets the lease test use the very
    ``sessions(n, per_statement_connection=True)`` fixture the concurrency
    lane wrote, instead of a second copy that could drift from it.
    """
    source = Path(__file__).resolve().parent.parent / "concurrency" / "conftest.py"
    spec = importlib.util.spec_from_file_location("files_concurrency_kit", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_concurrency_kit = _load_concurrency_kit()
# `sessions` borrows its connections from the kit's engine, so both fixtures
# have to be visible where `sessions` is requested.
concurrency_engine = _concurrency_kit.concurrency_engine
sessions = _concurrency_kit.sessions


class MtimeAges:
    """The age horizon, backed by the filesystem the store writes into.

    A real deployment reads the driver's listing timestamps. Knowing that this
    one is a directory tree is a *fixture's* business, not the library's: the
    janitor only ever sees the ``ObjectAgeSource`` protocol.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._overrides: dict[str, datetime] = {}

    def set(self, key: str, when: datetime) -> None:
        """Pin an object's write time, so a test can age it deliberately."""
        self._overrides[key] = when

    async def written_at(self, key: str) -> datetime | None:
        pinned = self._overrides.get(key)
        if pinned is not None:
            return pinned
        path = self._root / key
        if not path.exists():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


@dataclass(frozen=True, slots=True)
class Domain:
    """The store side of one dedup domain, plus the paths under it."""

    id: DomainId
    root: Path
    store: FilesystemStore
    ages: MtimeAges

    def _base(self) -> Path:
        return self.root / "domains" / str(self.id)

    def write(self, relative: str, payload: bytes) -> str:
        """Put bytes at a domain-relative key, the way a writer would."""
        path = self._base() / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return relative

    def absolute(self, relative: str) -> str:
        return f"domains/{self.id}/{relative}"

    def exists(self, relative: str) -> bool:
        return (self._base() / relative).exists()

    def listing(self, relative_prefix: str) -> set[str]:
        """Every domain-relative key under a prefix, as it is on disk."""
        start = self._base() / relative_prefix
        if not start.exists():
            return set()
        return {str(p.relative_to(self._base())) for p in start.rglob("*") if p.is_file()}


@pytest.fixture
def domain(tmp_path: Path, clock: FakeClock) -> Domain:
    """An admin-rooted store over a scratch tree, and one domain inside it."""
    root = tmp_path / "bucket"
    store = FilesystemStore(root, clock=clock, layout="bucket")
    return Domain(id=DomainId(uuid.uuid4()), root=root, store=store, ages=MtimeAges(root))


@pytest.fixture
def repo_for_org(files_session: AsyncSession) -> Callable[[OrgScope], FilesRepo]:
    """The janitor's repo factory: one repo per org, on the test's session."""

    def make(scope: OrgScope) -> FilesRepo:
        return FilesRepo(files_session, scope)

    return make


@pytest.fixture
async def shard(files_session: AsyncSession) -> AsyncIterator[int]:
    """A private shard row, so parallel tests never contend for one number."""
    number = uuid.uuid4().int % 1_000_000 + 1_000
    files_session.add(FileSweepShard(shard=number, cursor={}))
    await files_session.commit()
    yield number


class Seeder:
    """Seeds the rows that make an object reachable, one root at a time."""

    def __init__(self, session: AsyncSession, org: Any, drive: Any) -> None:
        self._session = session
        self._org = org
        self.drive = drive

    async def version(
        self,
        node: FileNode,
        store_key: str,
        *,
        size: int = 1_000_000,
        held: bool = False,
        seq: int = 1,
    ) -> FileVersion:
        version = FileVersion(
            id=uuid.uuid4(),
            org_team_id=self._org.org_team_id,
            node_id=node.id,
            seq=seq,
            size_bytes=size,
            content_hash=uuid.uuid4().hex,
            store_key=store_key,
            held=held,
            source="upload",
        )
        self._session.add(version)
        await self._session.commit()
        return version

    async def trash(self, node: FileNode, purge_after: datetime) -> FileTrashOp:
        op = FileTrashOp(
            id=uuid.uuid4(),
            org_team_id=self._org.org_team_id,
            drive_id=self.drive.id,
            root_node_id=node.id,
            actor_id=self._org.admin_id,
            purge_after=purge_after,
        )
        self._session.add(op)
        await self._session.flush()
        node.trashed_at = purge_after - timedelta(days=30)
        node.trash_op_id = op.id
        await self._session.commit()
        return op

    async def upload_session(self, parent: FileNode, *, state: str = "open") -> FileUploadSession:
        row = FileUploadSession(
            id=uuid.uuid4(),
            org_team_id=self._org.org_team_id,
            drive_id=self.drive.id,
            parent_id=parent.id,
            name=b"pending.bin",
            dedup_domain_id=self.drive.dedup_domain_id,
            state=state,
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        self._session.add(row)
        await self._session.commit()
        return row

    async def grant(self, version: FileVersion, expires_at: datetime) -> FileContentGrant:
        row = FileContentGrant(
            nonce=uuid.uuid4().hex,
            org_team_id=self._org.org_team_id,
            version_id=version.id,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.commit()
        return row


@pytest.fixture
async def seeded(
    files_session: AsyncSession,
    files_org: Any,
    files_factory: Any,
) -> tuple[Seeder, dict[str, FileNode]]:
    """A drive with two files, and the seeder that makes their bytes matter."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("keep.bin drop.bin", drive=drive)
    return Seeder(files_session, files_org, drive), tree

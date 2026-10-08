"""The rig every content test writes and reads through.

Real Postgres on this run's lane database, a real ``FilesystemStore`` wrapped in
``FaultyStore`` so "the object is there" is read off the disk and "how many store
calls" is read off the driver's own log — never off a mock.
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
from alkera_core.files.content import ContentService
from alkera_core.files.ids import DomainId, DriveId, NodeId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.models.files.tree import FileNode
from alkera_test_support.files.faulty_store import FaultSchedule, FaultyStore
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg


def content_ctx() -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER, id=str(uuid.uuid4()), org_id=uuid.uuid4()
        )
    )


async def stream(payload: bytes, *, chunk: int = 7) -> AsyncIterator[bytes]:
    """The bytes as a client sends them: many chunks, never one buffer."""
    for start in range(0, len(payload), chunk):
        yield payload[start : start + chunk]
    if not payload:
        return


@dataclass(frozen=True, slots=True)
class ContentRig:
    service: ContentService
    #: The one principal this rig acts as. A lease taken in a test is taken by
    #: the same caller the service writes as, because that is the only shape a
    #: request has — and the fence now matches the holder on the row, so a rig
    #: that minted a fresh principal per collaborator could never write under
    #: its own lease.
    ctx: ActingContext
    repo: FilesRepo
    store: FaultyStore
    root: Path
    domain_id: DomainId
    drive_uuid: uuid.UUID
    files: dict[str, FileNode]
    checkpoints: PausingCheckpoints
    session: AsyncSession
    org: FilesOrg

    @property
    def drive_id(self) -> DriveId:
        return DriveId(self.drive_uuid)

    def node_id(self, name: str) -> NodeId:
        return NodeId(self.files[name].id)

    def object_path(self, key: str) -> Path:
        return self.root / "domains" / str(self.domain_id) / key


@pytest.fixture
async def content_rig(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    checkpoints: PausingCheckpoints,
    tmp_path: Path,
) -> ContentRig:
    drive = await files_factory.drive()
    tree = await files_factory.tree("papers/ papers/a.bin papers/b.bin", drive=drive)
    ctx = content_ctx()
    domain_id = DomainId(drive.dedup_domain_id)
    domain_root = tmp_path / "domains" / str(domain_id)
    inner = FaultyStore(FilesystemStore(domain_root, clock=clock.now), FaultSchedule(faults=()))
    store = _RootedDomainStore(inner, domain_id)
    repo = FilesRepo(files_session, OrgScope(org_team_id=files_org.org_team_id))
    return ContentRig(
        service=ContentService(repo, ctx, clock, store, checkpoints=checkpoints),
        ctx=ctx,
        repo=repo,
        store=inner,
        root=tmp_path,
        domain_id=domain_id,
        drive_uuid=drive.id,
        files={"a.bin": tree["papers/a.bin"], "b.bin": tree["papers/b.bin"]},
        checkpoints=checkpoints,
        session=files_session,
        org=files_org,
    )

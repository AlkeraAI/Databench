"""One rule for the outbox ``version``: the node's etag *after* the change.

A consumer of the Files feed builds an etag chain out of these rows — it holds
the last version it saw for a node and skips a frame it has already applied. So
the number in the row has to be the number a read of that node returns right
after the change: an emitter that announces the etag it read *before* writing
publishes a version that no read will ever return, and the chain breaks at that
node forever.

Every emitter is checked the same way and against the database, not against the
emitter's own arithmetic: run the real verb, then read the node's ``etag``
column back with plain SQL and compare it to the ``version`` on the outbox row
the verb wrote. Change any emitter to announce the pre-change etag (or that
etag plus one where it is already correct) and this module's row for that verb
fails.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.events.types import EventType
from alkera_core.files import acl, conflict_resolution, stars
from alkera_core.files.clock import FakeClock
from alkera_core.files.content import ContentService
from alkera_core.files.copy import load_copy_plan, run_copy, start_copy
from alkera_core.files.ids import DomainId, DriveId, NodeId, TrashOpId
from alkera_core.files.namespace import Namespace
from alkera_core.files.ops import Operations
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.files.trash import Trash
from alkera_core.models.event_outbox import EventOutbox
from alkera_core.models.files.history import FileConflict
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from alkera_test_support.files.faulty_store import FaultSchedule, FaultyStore
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True, slots=True)
class Rig:
    """Everything a verb needs to run one real mutation on one real node."""

    repo: FilesRepo
    session: AsyncSession
    org: FilesOrg
    factory: FilesFactory
    clock: FakeClock
    tmp_path: Path

    @property
    def ctx(self) -> ActingContext:
        return ActingContext(
            acting_principal=Principal(
                kind=PrincipalKind.USER,
                id=str(self.org.admin_id),
                org_id=self.org.org_team_id,
                credential=CredentialKind.JWT,
            )
        )


#: A verb: run one mutation, return the id of the node whose etag it announced.
Verb = Callable[[Rig], Awaitable[uuid.UUID]]


async def _etag(session: AsyncSession, node_id: uuid.UUID) -> int:
    """The node's etag as a later reader sees it — plain SQL, no identity map."""
    read = await session.execute(
        text("SELECT etag FROM file_nodes WHERE id = :id"), {"id": node_id}
    )
    value = read.scalar_one_or_none()
    assert value is not None, f"node {node_id} vanished"
    return int(value)


async def _announced(session: AsyncSession, node_id: uuid.UUID) -> int:
    """The ``version`` on the last ``file.node.changed`` row for this node."""
    rows = await session.execute(
        select(EventOutbox)
        .where(
            EventOutbox.entity_id == str(node_id),
            EventOutbox.type == EventType.FILE_NODE_CHANGED,
        )
        .order_by(EventOutbox.id)
    )
    written = list(rows.scalars().all())
    assert written, f"no outbox row announced node {node_id}"
    last = written[-1]
    # The column and the payload are two spellings of the same claim; a
    # consumer may read either, so both have to carry the same number.
    assert last.payload["version"] == last.version
    return int(last.version)


async def _stream(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def _namespace(rig: Rig) -> Namespace:
    return Namespace(rig.repo, rig.ctx, rig.clock, None)


async def _fresh(rig: Rig, node_id: uuid.UUID) -> FileNode:
    """The node as it stands now, around the session's cached copy."""
    async with rig.repo.transaction():
        node = await rig.repo.node(NodeId(node_id))
    assert node is not None
    await rig.session.refresh(node)
    return node


# ---- the verbs ------------------------------------------------------------


async def _verb_create(rig: Rig) -> uuid.UUID:
    drive = await rig.factory.drive()
    assert drive.root_node_id is not None
    async with rig.repo.transaction():
        made = await _namespace(rig).create(
            DriveId(drive.id), NodeId(drive.root_node_id), "file", b"made.txt"
        )
    return made.id


async def _verb_rename(rig: Rig) -> uuid.UUID:
    drive = await rig.factory.drive()
    node = (await rig.factory.tree("a.txt", drive=drive))["a.txt"]
    async with rig.repo.transaction():
        await _namespace(rig).rename(NodeId(node.id), b"b.txt", if_match=node.etag)
    return node.id


async def _verb_move(rig: Rig) -> uuid.UUID:
    drive = await rig.factory.drive()
    made = await rig.factory.tree("dst/ a.txt", drive=drive)
    node, dst = made["a.txt"], made["dst"]
    async with rig.repo.transaction():
        await _namespace(rig).move(NodeId(node.id), NodeId(dst.id), if_match=node.etag)
    return node.id


async def _verb_trash(rig: Rig) -> uuid.UUID:
    drive = await rig.factory.drive()
    node = (await rig.factory.tree("a.txt", drive=drive))["a.txt"]
    async with rig.repo.transaction():
        await Trash(rig.repo, rig.ctx, rig.clock, None).trash(NodeId(node.id), if_match=node.etag)
    return node.id


async def _verb_restore(rig: Rig) -> uuid.UUID:
    drive = await rig.factory.drive()
    node = (await rig.factory.tree("a.txt", drive=drive))["a.txt"]
    trash = Trash(rig.repo, rig.ctx, rig.clock, None)
    async with rig.repo.transaction():
        op = await trash.trash(NodeId(node.id), if_match=node.etag)
    async with rig.repo.transaction():
        await trash.restore(TrashOpId(op.id))
    return node.id


async def _verb_star(rig: Rig) -> uuid.UUID:
    drive = await rig.factory.drive()
    node = (await rig.factory.tree("a.txt", drive=drive))["a.txt"]
    async with rig.repo.transaction():
        assert await stars.star(rig.repo, rig.ctx, NodeId(node.id)) is True
    return node.id


def _member(rig: Rig) -> Principal:
    return Principal(
        kind=PrincipalKind.USER,
        id=str(rig.org.member_id),
        org_id=rig.org.org_team_id,
        credential=CredentialKind.JWT,
    )


async def _verb_grant(rig: Rig) -> uuid.UUID:
    drive = await rig.factory.drive()
    node = (await rig.factory.tree("a.txt", drive=drive))["a.txt"]
    async with rig.repo.transaction():
        await acl.grant(rig.repo, rig.ctx, node, _member(rig), "reader")
    return node.id


async def _verb_revoke(rig: Rig) -> uuid.UUID:
    drive = await rig.factory.drive()
    node = (await rig.factory.tree("a.txt", drive=drive))["a.txt"]
    async with rig.repo.transaction():
        share = await acl.grant(rig.repo, rig.ctx, node, _member(rig), "reader")
    current = await _fresh(rig, node.id)
    async with rig.repo.transaction():
        await acl.revoke(rig.repo, rig.ctx, current, share.id)
    return node.id


async def _verb_resolve(rig: Rig) -> uuid.UUID:
    drive = await rig.factory.drive()
    node = (await rig.factory.tree("doc.txt", drive=drive))["doc.txt"]
    versions = [
        FileVersion(
            id=uuid.uuid4(),
            org_team_id=node.org_team_id,
            node_id=node.id,
            seq=seq,
            size_bytes=size,
            content_hash=digest * 32,
            source="upload",
            store_key=f"objects/{digest * 32}",
        )
        for seq, size, digest in ((1, 11, "aa"), (2, 22, "bb"))
    ]
    for version in versions:
        rig.session.add(version)
    await rig.session.flush()
    node.head_version_id = versions[0].id
    rig.session.add(
        FileConflict(
            id=(conflict_id := uuid.uuid4()),
            org_team_id=rig.org.org_team_id,
            node_id=node.id,
            base_version_id=None,
            theirs_version_id=versions[0].id,
            mine_version_id=versions[1].id,
            actor=rig.org.admin_id,
            state="open",
        )
    )
    await rig.session.commit()
    async with rig.repo.transaction():
        await conflict_resolution.resolve_conflict(
            rig.repo,
            rig.ctx,
            conflict_id,
            choice="mine",
            if_match=node.etag,
            clock=rig.clock,
        )
    return node.id


async def _verb_copy(rig: Rig) -> uuid.UUID:
    drive = await rig.factory.drive()
    made = await rig.factory.tree("src/ src/a.txt dst/", drive=drive)
    ops = Operations(rig.repo, rig.ctx, rig.clock)
    async with rig.repo.transaction():
        op_id = await start_copy(rig.repo, ops, node=made["src"], dest_parent=made["dst"])
    await run_copy(rig.repo, rig.ctx, op_id, clock=rig.clock)
    plan = await load_copy_plan(rig.repo, op_id)
    assert plan.new_root_id is not None
    return uuid.UUID(str(plan.new_root_id))


async def _verb_put_version(rig: Rig) -> uuid.UUID:
    drive = await rig.factory.drive()
    node = (await rig.factory.tree("a.bin", drive=drive))["a.bin"]
    domain_id = DomainId(drive.dedup_domain_id)
    inner = FaultyStore(
        FilesystemStore(rig.tmp_path / "domains" / str(domain_id), clock=rig.clock.now),
        FaultSchedule(faults=()),
    )
    service = ContentService(rig.repo, rig.ctx, rig.clock, _RootedDomainStore(inner, domain_id))
    await service.put_version(
        NodeId(node.id), _stream(b"payload"), size_declared=7, if_match=node.etag
    )
    return node.id


VERBS: list[tuple[str, Verb]] = [
    ("create", _verb_create),
    ("rename", _verb_rename),
    ("move", _verb_move),
    ("trash", _verb_trash),
    ("restore", _verb_restore),
    ("put_version", _verb_put_version),
    ("grant", _verb_grant),
    ("revoke", _verb_revoke),
    ("resolve", _verb_resolve),
    ("copy", _verb_copy),
    ("star", _verb_star),
]


@pytest.mark.parametrize("verb", [pytest.param(run, id=name) for name, run in VERBS])
async def test_the_announced_version_is_the_etag_a_later_read_returns(
    verb: Verb,
    repo: FilesRepo,
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    tmp_path: Path,
) -> None:
    """Every emitter announces the post-change etag, so the chain never breaks."""
    rig = Rig(repo, files_session, files_org, files_factory, clock, tmp_path)
    node_id = await verb(rig)

    announced = await _announced(files_session, node_id)
    assert announced == await _etag(files_session, node_id)


async def test_a_chain_of_changes_announces_every_etag_with_no_gap(
    repo: FilesRepo,
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
) -> None:
    """Three emitters in a row hand a consumer a contiguous etag sequence.

    This is the property the rule exists for and the one an off-by-one breaks
    even when each row is individually plausible: rename, then star, then
    grant, and the versions the feed carries must be exactly the etags the node
    passed through — no repeat, no skip.
    """
    rig = Rig(repo, files_session, files_org, files_factory, clock, Path())
    drive = await files_factory.drive()
    node = (await files_factory.tree("a.txt", drive=drive))["a.txt"]

    seen: list[int] = []
    async with repo.transaction():
        await _namespace(rig).rename(NodeId(node.id), b"b.txt", if_match=node.etag)
    seen.append(await _etag(files_session, node.id))
    async with repo.transaction():
        await stars.star(repo, rig.ctx, NodeId(node.id))
    seen.append(await _etag(files_session, node.id))
    current = await _fresh(rig, node.id)
    async with repo.transaction():
        await acl.grant(repo, rig.ctx, current, _member(rig), "reader")
    seen.append(await _etag(files_session, node.id))

    rows = await files_session.execute(
        select(EventOutbox)
        .where(
            EventOutbox.entity_id == str(node.id),
            EventOutbox.type == EventType.FILE_NODE_CHANGED,
        )
        .order_by(EventOutbox.id)
    )
    announced = [int(row.version) for row in rows.scalars().all()]
    assert announced == seen

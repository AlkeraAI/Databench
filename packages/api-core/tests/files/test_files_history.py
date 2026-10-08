"""History rows and outbox rows land — and vanish — with the change itself."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, get_args

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.events.types import EventType, RealtimeEventType
from alkera_core.files import history
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DomainId, DriveId, NodeId, OperationId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.event_outbox import EventOutbox
from alkera_core.models.files.history import FileHistory
from alkera_core.models.files.tree import FileNode
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: Payload keys that would put the outbox on the erasure path.
NEVER_AUDITED_KEYS = frozenset({"name", "name_display", "path", "symlink_target", "content"})


def _user_ctx(org: FilesOrg) -> ActingContext:
    user = Principal(
        kind=PrincipalKind.USER,
        id=str(org.admin_id),
        org_id=org.org_team_id,
        credential=CredentialKind.JWT,
    )
    return ActingContext(acting_principal=user)


def _agent_ctx(org: FilesOrg, session_id: uuid.UUID) -> ActingContext:
    user = Principal(kind=PrincipalKind.USER, id=str(org.member_id), org_id=org.org_team_id)
    agent = Principal(kind=PrincipalKind.AGENT, id=str(session_id), org_id=org.org_team_id)
    return ActingContext(
        acting_principal=agent, delegating_user=user, delegation_chain=(user, agent)
    )


async def _leaf(files_factory: FilesFactory, drive: Any) -> FileNode:
    made = await files_factory.tree("f/ f/a.txt", drive=drive)
    return made["f/a.txt"]


async def _outbox_rows(session: AsyncSession, org: FilesOrg) -> list[EventOutbox]:
    rows = await session.execute(
        select(EventOutbox).where(EventOutbox.org_id == org.org_team_id).order_by(EventOutbox.id)
    )
    return list(rows.scalars().all())


async def test_history_and_outbox_commit_with_the_node_update(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """All three effects are one transaction: the row, its history, its event."""
    drive = await files_factory.drive()
    node = await _leaf(files_factory, drive)
    ctx = _user_ctx(files_org)

    async with repo.transaction():
        await repo.session.execute(update(FileNode).where(FileNode.id == node.id).values(size=99))
        await history.record(
            repo,
            ctx,
            node_id=NodeId(node.id),
            kind="attrs",
            before={"size": 0},
            after={"size": 99},
        )
        await history.emit_node_changed(
            repo, ctx, node_id=NodeId(node.id), drive_id=DriveId(drive.id), version=1
        )

    rows = (
        (await files_session.execute(select(FileHistory).where(FileHistory.node_id == node.id)))
        .scalars()
        .all()
    )
    assert [(r.seq, r.kind, r.after) for r in rows] == [(1, "attrs", {"size": 99})]
    events = await _outbox_rows(files_session, files_org)
    assert [e.type for e in events] == [EventType.FILE_NODE_CHANGED.value]
    assert (
        await files_session.execute(select(FileNode.size).where(FileNode.id == node.id))
    ).scalar_one() == 99


async def test_a_rollback_takes_the_history_row_and_the_event_with_it(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """No history without the change, and no announcement of a change that
    did not happen."""
    drive = await files_factory.drive()
    node = await _leaf(files_factory, drive)
    node_id, drive_id = node.id, drive.id
    ctx = _user_ctx(files_org)

    boom = RuntimeError("the mutation failed after its history was written")
    with pytest.raises(RuntimeError) as raised:
        async with repo.transaction():
            await repo.session.execute(
                update(FileNode).where(FileNode.id == node_id).values(size=77)
            )
            await history.record(
                repo, ctx, node_id=NodeId(node_id), kind="attrs", before=None, after={"size": 77}
            )
            await history.emit_node_changed(
                repo, ctx, node_id=NodeId(node_id), drive_id=DriveId(drive_id), version=1
            )
            raise boom
    assert raised.value is boom

    assert (
        await files_session.execute(select(FileHistory).where(FileHistory.node_id == node_id))
    ).scalars().all() == []
    assert await _outbox_rows(files_session, files_org) == []
    assert (
        await files_session.execute(select(FileNode.size).where(FileNode.id == node_id))
    ).scalar_one() == 0


async def test_seq_is_dense_per_node_under_two_concurrent_writers(
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    files_engine: AsyncEngine,
) -> None:
    """Two backends racing the same node's history produce 1 and 2, never two 1s.

    They are two real connections: on one session SQLAlchemy would serialize
    the statements and the race could not happen at all. Both overlap in the
    gather below, so the pool cannot hand them the same one.
    """
    drive = await files_factory.drive()
    node = await _leaf(files_factory, drive)
    ctx = _user_ctx(files_org)
    left = AsyncSession(bind=files_engine, expire_on_commit=False)
    right = AsyncSession(bind=files_engine, expire_on_commit=False)

    async def write(session: AsyncSession, kind: history.HistoryKind) -> None:
        writer = FilesRepo(session, files_org.scope)
        async with writer.transaction():
            await history.record(
                writer, ctx, node_id=NodeId(node.id), kind=kind, before=None, after=None
            )

    try:
        # Both compute their seq from the same committed maximum. The unique
        # index lets exactly one of them keep it; the loser recomputes, which
        # is what makes the sequence dense rather than duplicated.
        await asyncio.gather(write(left, "create"), write(right, "attrs"))
    finally:
        await left.close()
        await right.close()

    seqs = (
        (
            await files_session.execute(
                select(FileHistory.seq)
                .where(FileHistory.node_id == node.id)
                .order_by(FileHistory.seq)
            )
        )
        .scalars()
        .all()
    )
    assert list(seqs) == [1, 2]


async def test_the_row_carries_the_agent_delegation_chain(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """An agent acting for a user is on the row, not inferred from it later."""
    drive = await files_factory.drive()
    node = await _leaf(files_factory, drive)
    session_id = uuid.uuid4()

    async with repo.transaction():
        await history.record(
            repo,
            _agent_ctx(files_org, session_id),
            node_id=NodeId(node.id),
            kind="rename",
            before=None,
            after=None,
        )
    row = (
        await files_session.execute(select(FileHistory).where(FileHistory.node_id == node.id))
    ).scalar_one()
    assert (row.acting_principal, row.delegating_user, row.agent_session_id) == (
        session_id,
        files_org.member_id,
        session_id,
    )


async def test_a_direct_user_leaves_no_delegation_on_the_row(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The negative twin: no agent, no session, no delegating user."""
    drive = await files_factory.drive()
    node = await _leaf(files_factory, drive)
    async with repo.transaction():
        await history.record(
            repo,
            _user_ctx(files_org),
            node_id=NodeId(node.id),
            kind="create",
            before=None,
            after=None,
        )
    row = (
        await files_session.execute(select(FileHistory).where(FileHistory.node_id == node.id))
    ).scalar_one()
    assert (row.acting_principal, row.delegating_user, row.agent_session_id) == (
        files_org.admin_id,
        None,
        None,
    )


@pytest.mark.parametrize(
    "emitter",
    [pytest.param("node", id="file_node.changed"), pytest.param("op", id="file_operation.changed")],
)
async def test_outbox_payloads_carry_ids_only(
    emitter: str,
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
) -> None:
    """A name or a path in an outbox row would put retention on the erasure path."""
    drive = await files_factory.drive()
    node = await _leaf(files_factory, drive)
    ctx = _user_ctx(files_org)
    async with repo.transaction():
        if emitter == "node":
            await history.emit_node_changed(
                repo, ctx, node_id=NodeId(node.id), drive_id=DriveId(drive.id), version=3
            )
        else:
            await history.emit_operation_changed(
                repo,
                ctx,
                op_id=OperationId(uuid.uuid4()),
                drive_id=DriveId(drive.id),
                version=3,
            )
    (row,) = await _outbox_rows(files_session, files_org)
    assert set(row.payload) & NEVER_AUDITED_KEYS == set()
    assert node.name_display not in repr(row.payload)
    # Ids, a number, and — the single exception — a fixed word from a closed
    # set, which a client branches on and never renders. Anything else here is
    # free text, and free text in an outbox row is on the erasure path.
    assert all(k.endswith("_id") or k in {"version", "reason"} for k in row.payload)
    assert row.payload.get("reason") in {None, *get_args(history.NodeChangeReason)}
    assert row.version == 3


async def test_history_refuses_a_kind_the_table_would_refuse(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    drive = await files_factory.drive()
    node = await _leaf(files_factory, drive)
    async with repo.transaction():
        with pytest.raises(ValueError, match="unknown history kind"):
            await history.record(
                repo,
                _user_ctx(files_org),
                node_id=NodeId(node.id),
                kind="deleted",  # type: ignore[arg-type]
                before=None,
                after=None,
            )


async def test_writing_outside_a_transaction_is_refused(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """Outside `transaction()` the app role and the org GUC are not in force."""
    from alkera_core.files.repo import RepoUsageError

    drive = await files_factory.drive()
    node = await _leaf(files_factory, drive)
    with pytest.raises(RepoUsageError):
        await history.record(
            repo,
            _user_ctx(files_org),
            node_id=NodeId(node.id),
            kind="create",
            before=None,
            after=None,
        )
    with pytest.raises(RepoUsageError):
        await history.emit_node_changed(
            repo,
            _user_ctx(files_org),
            node_id=NodeId(node.id),
            drive_id=DriveId(drive.id),
            version=0,
        )


def test_both_files_events_are_client_deliverable() -> None:
    """The portal invalidates its Files queries off these, so they must be on
    the realtime enum as well as the registry."""
    assert RealtimeEventType.FILE_NODE_CHANGED.value == EventType.FILE_NODE_CHANGED.value
    assert RealtimeEventType.FILE_OPERATION_CHANGED.value == (
        EventType.FILE_OPERATION_CHANGED.value
    )
    assert {"file_node.changed", "file_operation.changed"} <= {m.value for m in RealtimeEventType}


async def test_the_history_row_is_visible_to_the_app_role(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """The write runs as `alkera_files_app` under the org GUC, so RLS binds."""
    drive = await files_factory.drive()
    node = await _leaf(files_factory, drive)
    async with repo.transaction():
        await history.record(
            repo,
            _user_ctx(files_org),
            node_id=NodeId(node.id),
            kind="create",
            before=None,
            after=None,
        )
        role = (await repo.session.execute(text("SELECT current_user"))).scalar_one()
        assert role == "alkera_files_app"


# ---- an agent whose id is not a UUID (F-191) -------------------------------


def _agent_ctx_named(org: FilesOrg, agent_id: str) -> ActingContext:
    """An agent link whose id is an opaque session name rather than a UUID."""
    user = Principal(kind=PrincipalKind.USER, id=str(org.member_id), org_id=org.org_team_id)
    agent = Principal(
        kind=PrincipalKind.AGENT,
        id=agent_id,
        org_id=org.org_team_id,
        credential=CredentialKind.AGENT_HEADER,
    )
    return ActingContext(
        acting_principal=agent, delegating_user=user, delegation_chain=(user, agent)
    )


@pytest.mark.parametrize(
    "agent_id",
    [
        pytest.param("sess-7f3a", id="opaque-name"),
        pytest.param("chat/2026-09-09/17", id="path-shaped"),
        pytest.param("00000000-0000-0000-0000-00000000000", id="one-hex-digit-short"),
    ],
)
def test_actor_ref_folds_a_non_uuid_principal_deterministically(
    files_org: FilesOrg, agent_id: str
) -> None:
    """A principal id that is not a UUID still gets one, and always the same one.

    `search.recent` matches `file_history.acting_principal` against this value,
    so a second call must not invent a second actor for the same agent.
    """
    ctx = _agent_ctx_named(files_org, agent_id)
    first = history.actor_ref(ctx)
    assert first == history.actor_ref(_agent_ctx_named(files_org, agent_id))
    assert first != history.actor_ref(_agent_ctx_named(files_org, agent_id + "x"))
    assert first != uuid.UUID(str(files_org.member_id)), "the fold must not collide with a user"


def test_actor_ref_keeps_a_uuid_principal_id_unchanged(files_org: FilesOrg) -> None:
    """A user (and an agent whose session id happens to be a UUID) keeps its id."""
    assert history.actor_ref(_user_ctx(files_org)) == files_org.admin_id
    session_id = uuid.uuid4()
    assert history.actor_ref(_agent_ctx(files_org, session_id)) == session_id


async def test_a_non_uuid_agent_records_history_on_a_fresh_org_drive(
    files_session: AsyncSession, files_org: FilesOrg, repo: FilesRepo
) -> None:
    """F-191: the first touch of an org's drive by an agent must not 500.

    `drives.ensure_org_drive` -> `_ensure_root` -> `_announce` writes the very
    first history row of the org, so a history writer that assumed a UUID
    principal id took down every Files route for an agent in a fresh org.
    """
    from alkera_core.files import drives
    from alkera_core.models.files.stores import FileStore

    store = FileStore(
        id=uuid.uuid4(),
        driver="filesystem",
        bucket="",
        endpoint=f"/tmp/files-test/{uuid.uuid4().hex}",
        region="",
        capabilities={},
        transfer_modes=["single"],
    )
    files_session.add(store)
    await files_session.commit()

    ctx = _agent_ctx_named(files_org, "sess-7f3a")
    async with repo.transaction():
        drive = await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store.id)
        assert drive.root_node_id is not None

    rows = await files_session.execute(
        select(FileHistory).where(FileHistory.org_team_id == files_org.org_team_id)
    )
    written = list(rows.scalars().all())
    assert written, "the drive skeleton must leave history behind"
    expected = history.actor_ref(ctx)
    assert {row.acting_principal for row in written} == {expected}
    assert {row.delegating_user for row in written} == {files_org.member_id}


# ---- the SUBJECT of an action, when it is not a user (F-260) ---------------

#: A service principal's id is a component name, never a UUID.
SERVICE_ID = "files-janitor"


def _service_ctx(org: FilesOrg) -> ActingContext:
    """A service principal: no delegating user, and an id that is a name."""
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.SERVICE,
            id=SERVICE_ID,
            org_id=org.org_team_id,
            credential=CredentialKind.CI_TOKEN,
        )
    )


def _folded_service_ref() -> uuid.UUID:
    """What the service's subject reference must be, computed here rather than
    by calling the helper under test."""
    return uuid.uuid5(history.ACTOR_NAMESPACE, f"service:{SERVICE_ID}")


@dataclass
class _SubjectRig:
    repo: FilesRepo
    session: AsyncSession
    ctx: ActingContext
    clock: FakeClock
    drive: Any
    folder: FileNode
    leaf: FileNode
    store: Any


async def _column(
    session: AsyncSession, table: str, column: str, row_id: uuid.UUID
) -> uuid.UUID | None:
    """One persisted column, read back as a raw row."""
    found = await session.execute(
        text(f"SELECT {column} AS value FROM {table} WHERE id = :id"),
        {"id": row_id},
    )
    row = found.mappings().first()
    assert row is not None, f"{table} row {row_id} was not written"
    value = row["value"]
    return uuid.UUID(str(value)) if value is not None else None


async def _drive_namespace_create(rig: _SubjectRig) -> uuid.UUID | None:
    """``namespace.create`` stamps ``file_nodes.created_by``."""
    from alkera_core.files.namespace import Namespace

    async with rig.repo.transaction():
        made = await Namespace(rig.repo, rig.ctx, rig.clock, None).create(
            DriveId(rig.drive.id), NodeId(rig.folder.id), "file", b"made-by-a-service.txt"
        )
    return await _column(rig.session, "file_nodes", "created_by", made.id)


async def _drive_ops_start(rig: _SubjectRig) -> uuid.UUID | None:
    """``ops.start`` stamps ``file_ops.actor``, which is NOT NULL."""
    from alkera_core.files.ops import Operations

    async with rig.repo.transaction():
        state = await Operations(rig.repo, rig.ctx, rig.clock).start(
            "copy", drive_id=DriveId(rig.drive.id), total=1
        )
    return await _column(rig.session, "file_ops", "actor", state.id)


async def _drive_trash(rig: _SubjectRig) -> uuid.UUID | None:
    """``trash.trash`` stamps ``file_trash_ops.actor_id``."""
    from alkera_core.files.trash import Trash

    async with rig.repo.transaction():
        op = await Trash(rig.repo, rig.ctx, rig.clock, None).trash(
            NodeId(rig.leaf.id), if_match=rig.leaf.etag
        )
    return await _column(rig.session, "file_trash_ops", "actor_id", op.id)


async def _drive_upload_open(rig: _SubjectRig) -> uuid.UUID | None:
    """``uploads.open`` stamps ``file_upload_sessions.created_by``."""
    from alkera_core.files.uploads import UploadService

    opened = await UploadService(rig.repo, rig.ctx, rig.clock, rig.store).open(
        DriveId(rig.drive.id), NodeId(rig.folder.id), b"uploaded.bin", declared_size=4
    )
    return await _column(rig.session, "file_upload_sessions", "created_by", opened.id)


async def _drive_content_put(rig: _SubjectRig) -> uuid.UUID | None:
    """``content.put_version`` stamps ``file_versions.created_by``."""
    from alkera_core.files.content import ContentService

    async def body() -> AsyncIterator[bytes]:
        yield b"abcd"

    info = await ContentService(rig.repo, rig.ctx, rig.clock, rig.store).put_version(
        NodeId(rig.leaf.id), body(), size_declared=4, if_match=rig.leaf.etag
    )
    return await _column(rig.session, "file_versions", "created_by", info.id)


async def _drive_decider(rig: _SubjectRig) -> uuid.UUID | None:
    """The decider's caller identity: the id a lock holder is matched against.

    A service that holds the lock on a node must keep WRITE; before the fix its
    caller id was ``None``, which matched no holder, so its own lock fenced it.
    """
    from alkera_core.files.authz.decider import AccessFacts, LadderDecider

    async with rig.repo.transaction():
        drive = await rig.repo.drive(DriveId(rig.drive.id))
        locked = await rig.repo.node(NodeId(rig.leaf.id))
    assert drive is not None
    assert locked is not None
    # The decider is pure: it reads the node it is handed. Locking the row in
    # the database instead would only be read back through the session's
    # identity map, which still holds the pre-lock instance.
    rig.session.expunge(locked)
    locked.state = "locked"
    held = _folded_service_ref()
    access = LadderDecider().decide(
        rig.ctx,
        locked,
        [locked],
        [],
        drive,
        AccessFacts(org_admin=True, lock_holder_id=held),
    )
    assert access.allows(FilesAction.WRITE), "the lock holder keeps WRITE on its own lock"
    return held


@pytest.mark.parametrize(
    "drive_verb",
    [
        pytest.param(_drive_namespace_create, id="namespace-create"),
        pytest.param(_drive_ops_start, id="ops-start"),
        pytest.param(_drive_trash, id="trash-trash"),
        pytest.param(_drive_upload_open, id="uploads-open"),
        pytest.param(_drive_content_put, id="content-put-version"),
        pytest.param(_drive_decider, id="authz-decider"),
    ],
)
async def test_a_service_principal_is_the_subject_of_every_verb_that_names_one(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
    tmp_path: Any,
    drive_verb: Any,
) -> None:
    """F-260: a subject whose id is not a UUID is rendered, never assumed.

    Each verb here records the SUBJECT of the action - the delegating user when
    there is one, else the acting principal. A service principal has no
    delegating user and a component name for an id, so these sites used to
    crash (``ops``), write the nil UUID every service would share (``trash``)
    or drop the subject to NULL (``namespace``, ``uploads``, ``content``, and
    the decider's caller identity).
    """
    from alkera_core.files.store.filesystem import FilesystemStore
    from alkera_core.files.store.scoped import _RootedDomainStore

    drive = await files_factory.drive()
    tree = await files_factory.tree("box/ box/leaf.txt", drive=drive)
    domain_id = DomainId(drive.dedup_domain_id)
    store = _RootedDomainStore(
        FilesystemStore(tmp_path / "domains" / str(domain_id), clock=clock.now), domain_id
    )
    rig = _SubjectRig(
        repo=repo,
        session=files_session,
        ctx=_service_ctx(files_org),
        clock=clock,
        drive=drive,
        folder=tree["box"],
        leaf=tree["box/leaf.txt"],
        store=store,
    )

    recorded = await drive_verb(rig)

    assert recorded == _folded_service_ref(), "the subject is the service's own fold"
    assert recorded != uuid.UUID(int=0), "no service may share the nil actor"
    assert recorded != files_org.admin_id, "the fold must not collide with a user"

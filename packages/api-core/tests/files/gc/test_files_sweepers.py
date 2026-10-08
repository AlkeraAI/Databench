"""The reconciliation sweepers, one deadline at a time, against real Postgres.

Every row of the ledger gets the same two questions asked of it: one second
before its deadline the stale state must still be there (a sweeper that deletes
early loses data a user could still have asked for), and one second after it
must be gone. The two are separate parameters on purpose — a sweeper with an
inverted or missing comparison passes one of them and fails the other, and a
single "it eventually goes away" case would not tell them apart.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import sweepers
from alkera_core.files.checkpoints import CheckpointKilled, PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import Janitor
from alkera_core.files.idempotency import IDEMPOTENCY_RETENTION
from alkera_core.files.ids import DomainId, NodeId
from alkera_core.files.leases import DEFAULT_GRANT_DELAY
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.keys import deleted_key, object_key
from alkera_core.files.store.scoped import FilesystemScoped
from alkera_core.files.sweepers import (
    JANITOR_ORDER,
    AclRewriteStale,
    ConflictsAndQuarantine,
    ContentGrantNonces,
    HistoryCompaction,
    IdempotencyKeys,
    IncomingOrphans,
    LeaseReaper,
    MovingStale,
    OperationsRetention,
    PageGrantNonces,
    ReachabilityTarget,
    SweepDeps,
    Sweeper,
    TrashPurge,
    UnreferencedAcls,
    run_janitor,
)
from alkera_core.models.files.uploads import FileUploadSession
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

NOW = EPOCH + timedelta(days=400)
SECOND = timedelta(seconds=1)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


@dataclass
class Bed:
    """The seeded tenant plus the handles a planting function needs."""

    session: AsyncSession
    org: FilesOrg
    drive: Any
    node: Any
    repo: FilesRepo


@pytest.fixture
async def bed(files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory) -> Bed:
    drive = await files_factory.drive()
    tree = await files_factory.tree("doc.txt", drive=drive)
    return Bed(
        session=files_session,
        org=files_org,
        drive=drive,
        node=tree["doc.txt"],
        repo=FilesRepo(files_session, files_org.scope),
    )


def _deps(bed: Bed, **kwargs: Any) -> SweepDeps:
    return SweepDeps(repo=bed.repo, ctx=_ctx(bed.org), **kwargs)


async def _count(bed: Bed, sql: str, params: dict[str, Any] | None = None) -> int:
    row = (
        await bed.session.execute(text(sql), {"org": bed.org.org_team_id, **(params or {})})
    ).scalar_one()
    return int(row)


# -- the planted beds -------------------------------------------------------


async def _plant_idempotency(bed: Bed, planted_at: datetime) -> None:
    await bed.session.execute(
        text(
            "INSERT INTO file_idempotency_keys (org_team_id, key, principal_id, route, "
            "request_hash, status, body, created_at, expires_at) VALUES "
            "(:org, 'k1', :principal, '/files', 'h', 'succeeded', '{}'::jsonb, :at, :expires)"
        ),
        {
            "org": bed.org.org_team_id,
            "principal": bed.org.admin_id,
            "at": planted_at,
            # Planted as Files writes it: kept for Files' retention past its claim.
            "expires": planted_at + IDEMPOTENCY_RETENTION,
        },
    )
    await bed.session.commit()


async def _left_idempotency(bed: Bed) -> int:
    return await _count(bed, "SELECT count(*) FROM file_idempotency_keys WHERE org_team_id = :org")


async def _plant_nonce(bed: Bed, planted_at: datetime) -> None:
    version_id = await _version(bed)
    await bed.session.execute(
        text(
            "INSERT INTO file_content_grants (nonce, org_team_id, version_id, expires_at) "
            "VALUES (:nonce, :org, :version, :at)"
        ),
        {
            "nonce": uuid.uuid4().hex,
            "org": bed.org.org_team_id,
            "version": version_id,
            "at": planted_at,
        },
    )
    await bed.session.commit()


async def _left_nonce(bed: Bed) -> int:
    return await _count(bed, "SELECT count(*) FROM file_content_grants WHERE org_team_id = :org")


async def _plant_page_grant(bed: Bed, planted_at: datetime) -> None:
    await bed.session.execute(
        text(
            "INSERT INTO file_page_grants (id, org_team_id, nonce, root_node_id, "
            "entry_node_id, minted_by_user_id, expires_at) "
            "VALUES (:id, :org, :nonce, :node, :node, :user, :at)"
        ),
        {
            "id": uuid.uuid4(),
            "org": bed.org.org_team_id,
            "nonce": uuid.uuid4().hex,
            "node": bed.node.id,
            "user": bed.org.admin_id,
            "at": planted_at,
        },
    )
    await bed.session.commit()


async def _left_page_grants(bed: Bed) -> int:
    return await _count(bed, "SELECT count(*) FROM file_page_grants WHERE org_team_id = :org")


async def _version(bed: Bed) -> uuid.UUID:
    """A real version row through the ORM, so every default lands."""
    version = FileVersion(
        id=uuid.uuid4(),
        org_team_id=bed.org.org_team_id,
        node_id=bed.node.id,
        seq=_next_seq(),
        size_bytes=10,
        content_hash=uuid.uuid4().hex,
        store_key="objects/x",
        source="upload",
    )
    bed.session.add(version)
    await bed.session.flush()
    return version.id


_SEQ = iter(range(1, 10_000))


def _next_seq() -> int:
    return next(_SEQ)


async def _plant_op(bed: Bed, planted_at: datetime) -> None:
    await bed.session.execute(
        text(
            "INSERT INTO file_ops (id, org_team_id, drive_id, kind, actor, state, "
            "progress, errors, conflicts, result, created_at) VALUES "
            "(:id, :org, :drive, 'move', :actor, 'done', '{}'::jsonb, '[]'::jsonb, "
            "'[]'::jsonb, '{}'::jsonb, :at)"
        ),
        {
            "id": uuid.uuid4(),
            "org": bed.org.org_team_id,
            "drive": bed.drive.id,
            "actor": bed.org.admin_id,
            "at": planted_at,
        },
    )
    await bed.session.commit()


async def _left_ops(bed: Bed) -> int:
    return await _count(bed, "SELECT count(*) FROM file_ops WHERE org_team_id = :org")


async def _plant_conflict(bed: Bed, planted_at: datetime) -> None:
    theirs = await _version(bed)
    mine = await _version(bed)
    await bed.session.execute(
        text(
            "INSERT INTO file_conflicts (id, org_team_id, node_id, theirs_version_id, "
            "mine_version_id, actor, state, resolved_at) VALUES "
            "(:id, :org, :node, :theirs, :mine, :actor, 'resolved', :at)"
        ),
        {
            "id": uuid.uuid4(),
            "org": bed.org.org_team_id,
            "node": bed.node.id,
            "theirs": theirs,
            "mine": mine,
            "actor": bed.org.admin_id,
            "at": planted_at,
        },
    )
    await bed.session.commit()


async def _left_conflicts(bed: Bed) -> int:
    return await _count(bed, "SELECT count(*) FROM file_conflicts WHERE org_team_id = :org")


async def _plant_quarantine(bed: Bed, planted_at: datetime) -> None:
    await bed.session.execute(
        text(
            "INSERT INTO file_quarantine (id, org_team_id, kind, ref_id, reason, "
            "attempts, detail, resolved_at) VALUES "
            "(:id, :org, 'object', 'objects/x', 'no owner', 1, '{}'::jsonb, :at)"
        ),
        {"id": uuid.uuid4(), "org": bed.org.org_team_id, "at": planted_at},
    )
    await bed.session.commit()


async def _left_quarantine(bed: Bed) -> int:
    return await _count(bed, "SELECT count(*) FROM file_quarantine WHERE org_team_id = :org")


async def _plant_trash(bed: Bed, purge_after: datetime) -> None:
    op_id = uuid.uuid4()
    await bed.session.execute(
        text(
            "INSERT INTO file_trash_ops (id, org_team_id, drive_id, root_node_id, "
            "actor_id, purge_after) VALUES (:id, :org, :drive, :node, :actor, :at)"
        ),
        {
            "id": op_id,
            "org": bed.org.org_team_id,
            "drive": bed.drive.id,
            "node": bed.node.id,
            "actor": bed.org.admin_id,
            "at": purge_after,
        },
    )
    await bed.session.commit()


async def _plant_lease(bed: Bed, expires_at: datetime) -> None:
    await bed.session.execute(
        text(
            "INSERT INTO file_leases (node_id, org_team_id, epoch, holder_principal_kind, "
            "holder_principal_id, holder_instance_id, machine_id, purpose, expires_at) "
            "VALUES (:node, :org, 1, 'user', :holder, 'inst', 'm', 'mount', :at)"
        ),
        {
            "node": bed.node.id,
            "org": bed.org.org_team_id,
            "holder": bed.org.admin_id,
            "at": expires_at,
        },
    )
    await bed.session.commit()
    await _plant_lease_work(bed)


async def _plant_lease_work(bed: Bed) -> None:
    """The work a live lease drives: a stage job it owns and an upload session
    into the folder it holds. Both must survive a lease inside its TTL and both
    must end with the lease that lapses."""
    node_id, drive_id, domain_id = (
        await bed.session.execute(
            text(
                "SELECT n.id, n.drive_id, d.dedup_domain_id FROM file_nodes n "
                "JOIN file_drives d ON d.id = n.drive_id WHERE n.org_team_id = :org "
                "AND n.parent_id IS NOT NULL ORDER BY n.id LIMIT 1"
            ),
            {"org": bed.org.org_team_id},
        )
    ).one()
    await bed.session.execute(
        text(
            "INSERT INTO file_stage_jobs (id, org_team_id, lease_node_id, machine_id, "
            "direction, mode, state, cursor, bytes_total, bytes_done, failed_count, "
            "quarantined_count, lease_epoch) VALUES "
            "(:id, :org, :node, 'm', 'in', 'stage', 'running', '{}'::jsonb, 8, 0, 0, 0, 1)"
        ),
        {"id": uuid.uuid4(), "org": bed.org.org_team_id, "node": node_id},
    )
    await bed.session.execute(
        text(
            "INSERT INTO file_upload_sessions (id, org_team_id, drive_id, parent_id, name, "
            "dedup_domain_id, state, declared_size, bytes_received, transfer_mode, "
            "lease_epoch, quota_hold_bytes, quota_hold_nodes, expires_at) VALUES "
            "(:id, :org, :drive, :node, :name, :domain, 'uploading', 4096, 0, 'proxied', "
            "1, 4096, 1, :expires)"
        ),
        {
            "id": uuid.uuid4(),
            "org": bed.org.org_team_id,
            "drive": drive_id,
            "node": node_id,
            "name": b"from-the-box.bin",
            "domain": domain_id,
            "expires": NOW + timedelta(days=1),
        },
    )
    await bed.session.commit()


async def _verify_lease_reap(bed: Bed, side: str) -> None:
    """What reaping a lapsed lease has to leave behind, and what holding one has to.

    Three separate facts, because each fails on its own: a lapse that forgets
    `grantable_after` lets a new holder take the folder while the old holder's
    writes are still in flight; a lapse that leaves the stage job running leaves
    a job nobody can fence; and a lapse that leaves the session open leaves
    quota reserved against a holder that no longer exists.
    """
    bed.session.expire_all()
    row = (
        await bed.session.execute(
            text("SELECT grantable_after, reaped_at FROM file_leases WHERE org_team_id = :org"),
            {"org": bed.org.org_team_id},
        )
    ).one()
    job_state = (
        await bed.session.execute(
            text("SELECT state FROM file_stage_jobs WHERE org_team_id = :org"),
            {"org": bed.org.org_team_id},
        )
    ).scalar_one()
    session_state, held = (
        await bed.session.execute(
            text(
                "SELECT state, quota_hold_bytes FROM file_upload_sessions WHERE org_team_id = :org"
            ),
            {"org": bed.org.org_team_id},
        )
    ).one()

    if side == "before":
        assert row.reaped_at is None
        assert row.grantable_after <= NOW
        assert job_state == "running"
        assert (session_state, held) == ("uploading", 4096)
        return
    assert row.grantable_after == NOW + DEFAULT_GRANT_DELAY
    assert job_state == "cancelled"
    assert (session_state, held) == ("aborted", 0)


async def _live_leases(bed: Bed) -> int:
    return await _count(
        bed,
        "SELECT count(*) FROM file_leases WHERE org_team_id = :org AND reaped_at IS NULL",
    )


async def _plant_flag(bed: Bed, state: str, updated_at: datetime) -> None:
    await bed.session.execute(
        text("UPDATE file_nodes SET state = :state, updated_at = :at WHERE id = :id"),
        {"state": state, "at": updated_at, "id": bed.node.id},
    )
    await bed.session.commit()


async def _flagged(bed: Bed, state: str) -> int:
    return await _count(
        bed,
        "SELECT count(*) FROM file_nodes WHERE org_team_id = :org AND state = :state",
        {"state": state},
    )


async def _plant_history(bed: Bed, at: datetime) -> None:
    for seq in (1, 2, 3):
        await bed.session.execute(
            text(
                "INSERT INTO file_history (id, org_team_id, node_id, seq, kind, "
                "acting_principal, at) VALUES (:id, :org, :node, :seq, 'rename', :actor, :at)"
            ),
            {
                "id": uuid.uuid4(),
                "org": bed.org.org_team_id,
                "node": bed.node.id,
                "seq": seq,
                "actor": bed.org.admin_id,
                "at": at,
            },
        )
    await bed.session.commit()


async def _left_history(bed: Bed) -> int:
    return await _count(bed, "SELECT count(*) FROM file_history WHERE org_team_id = :org")


async def _plant_acl(bed: Bed, _at: datetime) -> None:
    await bed.session.execute(
        text(
            "INSERT INTO file_acls (id, org_team_id, body_hash, body) VALUES "
            "(:id, :org, :digest, '[]'::jsonb)"
        ),
        {"id": uuid.uuid4(), "org": bed.org.org_team_id, "digest": uuid.uuid4().hex},
    )
    await bed.session.commit()


async def _left_acls(bed: Bed) -> int:
    return await _count(bed, "SELECT count(*) FROM file_acls WHERE org_team_id = :org")


@dataclass(frozen=True)
class Row:
    """One ledger row: how to plant it, its sweeper, and what must survive."""

    sweeper: type[Sweeper]
    plant: Callable[[Bed, datetime], Awaitable[None]]
    left: Callable[[Bed], Awaitable[int]]
    #: The instant the planted row's deadline falls on, given `NOW`.
    stamp: Callable[[datetime], datetime]
    before: int
    after: int
    deps: Callable[[Bed], dict[str, Any]] = lambda bed: {}
    #: Extra facts the sweep must leave true, given which side of the deadline
    #: the row was planted on. `left`/`before`/`after` count survivors; this is
    #: for the rows whose reap also has to change something else.
    verify: Callable[[Bed, str], Awaitable[None]] | None = None


PURGED: list[NodeId] = []


def _trash_deps(bed: Bed) -> dict[str, Any]:
    async def purge(node_id: NodeId) -> None:
        PURGED.append(node_id)

    return {"purge_trash": purge}


ROWS: dict[str, Row] = {
    "idempotency_keys": Row(
        sweeper=IdempotencyKeys,
        plant=_plant_idempotency,
        left=_left_idempotency,
        stamp=lambda now: now - IDEMPOTENCY_RETENTION,
        before=1,
        after=0,
    ),
    "content_grant_nonces": Row(
        sweeper=ContentGrantNonces,
        plant=_plant_nonce,
        left=_left_nonce,
        stamp=lambda now: now - sweepers.NONCE_RETENTION,
        before=1,
        after=0,
    ),
    "page_grant_nonces": Row(
        sweeper=PageGrantNonces,
        plant=_plant_page_grant,
        left=_left_page_grants,
        stamp=lambda now: now - sweepers.NONCE_RETENTION,
        before=1,
        after=0,
    ),
    "operations_retention": Row(
        sweeper=OperationsRetention,
        plant=_plant_op,
        left=_left_ops,
        stamp=lambda now: now - sweepers.OPERATION_RETENTION,
        before=1,
        after=0,
    ),
    "conflicts": Row(
        sweeper=ConflictsAndQuarantine,
        plant=_plant_conflict,
        left=_left_conflicts,
        stamp=lambda now: now - sweepers.RESOLVED_RETENTION,
        before=1,
        after=0,
    ),
    "quarantine": Row(
        sweeper=ConflictsAndQuarantine,
        plant=_plant_quarantine,
        left=_left_quarantine,
        stamp=lambda now: now - sweepers.RESOLVED_RETENTION,
        before=1,
        after=0,
    ),
    "history_compaction": Row(
        sweeper=HistoryCompaction,
        plant=_plant_history,
        left=_left_history,
        stamp=lambda now: now - sweepers.HISTORY_HOT,
        before=3,
        after=1,
    ),
    "trash_purge": Row(
        sweeper=TrashPurge,
        plant=_plant_trash,
        left=lambda bed: _purged_count(),
        stamp=lambda now: now,
        before=0,
        after=1,
        deps=_trash_deps,
    ),
    "lease_reaper": Row(
        sweeper=LeaseReaper,
        plant=_plant_lease,
        left=_live_leases,
        stamp=lambda now: now,
        before=1,
        after=0,
        verify=_verify_lease_reap,
    ),
    "acl_rewrite_stale": Row(
        sweeper=AclRewriteStale,
        plant=lambda bed, at: _plant_flag(bed, "acl_rewriting", at),
        left=lambda bed: _flagged(bed, "acl_rewriting"),
        stamp=lambda now: now - sweepers.FLAG_STALE_AFTER,
        before=1,
        after=0,
    ),
    "moving_stale": Row(
        sweeper=MovingStale,
        plant=lambda bed, at: _plant_flag(bed, "moving", at),
        left=lambda bed: _flagged(bed, "moving"),
        stamp=lambda now: now - sweepers.FLAG_STALE_AFTER,
        before=1,
        after=0,
    ),
}


async def _purged_count() -> int:
    return len(PURGED)


@pytest.mark.parametrize("row_id", sorted(ROWS))
@pytest.mark.parametrize(
    ("offset", "side"),
    [
        pytest.param(SECOND, "before", id="one-second-before-the-deadline"),
        pytest.param(-SECOND, "after", id="one-second-after-the-deadline"),
    ],
)
async def test_every_row_is_swept_exactly_at_its_deadline(
    bed: Bed, row_id: str, offset: timedelta, side: str
) -> None:
    """A row planted just inside its window survives; just outside, it goes."""
    row = ROWS[row_id]
    PURGED.clear()
    # `offset` moves the planted stamp, not the clock: a stamp one second later
    # than the deadline is still inside the window, one second earlier is past.
    await row.plant(bed, row.stamp(NOW) + offset)
    outcome = await row.sweeper(_deps(bed, **row.deps(bed))).run(NOW)

    expected = row.before if side == "before" else row.after
    assert await row.left(bed) == expected
    assert (outcome.swept > 0) is (side == "after")
    if row.verify is not None:
        await row.verify(bed, side)


async def test_history_compaction_keeps_the_audit_kinds_whole(bed: Bed) -> None:
    """A cold create survives compaction; the renames beside it fold to one."""
    cold = NOW - sweepers.HISTORY_HOT - SECOND
    await _plant_history(bed, cold)
    await bed.session.execute(
        text(
            "INSERT INTO file_history (id, org_team_id, node_id, seq, kind, "
            "acting_principal, at) VALUES (:id, :org, :node, 0, 'create', :actor, :at)"
        ),
        {
            "id": uuid.uuid4(),
            "org": bed.org.org_team_id,
            "node": bed.node.id,
            "actor": bed.org.admin_id,
            "at": cold,
        },
    )
    await bed.session.commit()

    await HistoryCompaction(_deps(bed)).run(NOW)

    kinds = (
        await bed.session.execute(
            text("SELECT kind, after FROM file_history WHERE org_team_id = :org ORDER BY kind"),
            {"org": bed.org.org_team_id},
        )
    ).fetchall()
    assert [row[0] for row in kinds] == ["create", "rename"]
    assert kinds[1][1] == {"compacted": 3}


async def test_the_acl_sweeper_runs_weekly_and_spares_referenced_rows(bed: Bed) -> None:
    """Its cursor is the cadence: inside the week it declines to run at all."""
    await _plant_acl(bed, NOW)
    referenced = uuid.uuid4()
    await bed.session.execute(
        text(
            "INSERT INTO file_acls (id, org_team_id, body_hash, body) VALUES "
            "(:id, :org, :digest, '[]'::jsonb)"
        ),
        {"id": referenced, "org": bed.org.org_team_id, "digest": uuid.uuid4().hex},
    )
    await bed.session.execute(
        text("UPDATE file_nodes SET acl_id = :acl WHERE id = :id"),
        {"acl": referenced, "id": bed.node.id},
    )
    await bed.session.commit()

    first = await UnreferencedAcls(_deps(bed)).run(NOW)
    assert first.swept == 1
    assert await _left_acls(bed) == 1

    await _plant_acl(bed, NOW)
    too_soon = await UnreferencedAcls(_deps(bed)).run(
        NOW + sweepers.ACL_SWEEP_INTERVAL - SECOND, cursor=first.cursor
    )
    assert too_soon.skipped is True
    assert await _left_acls(bed) == 2

    due = await UnreferencedAcls(_deps(bed)).run(
        NOW + sweepers.ACL_SWEEP_INTERVAL + SECOND, cursor=first.cursor
    )
    assert due.swept == 1
    assert await _left_acls(bed) == 1


async def test_a_live_operation_keeps_a_moving_flag_standing(bed: Bed) -> None:
    """The flag is only stale when nothing is still driving the move."""
    await _plant_flag(bed, "moving", NOW - sweepers.FLAG_STALE_AFTER - SECOND)
    await bed.session.execute(
        text(
            "INSERT INTO file_ops (id, org_team_id, drive_id, kind, actor, state, "
            "progress, errors, conflicts, result, heartbeat_at) VALUES "
            "(:id, :org, :drive, 'move', :actor, 'running', '{}'::jsonb, '[]'::jsonb, "
            "'[]'::jsonb, '{}'::jsonb, :at)"
        ),
        {
            "id": uuid.uuid4(),
            "org": bed.org.org_team_id,
            "drive": bed.drive.id,
            "actor": bed.org.admin_id,
            "at": NOW,
        },
    )
    await bed.session.commit()

    outcome = await MovingStale(_deps(bed)).run(NOW)

    assert outcome.swept == 0
    assert await _flagged(bed, "moving") == 1


# -- the incoming prefix: cursors, and a kill in the middle of a page -------


class FakeIncoming:
    """An admin handle over a scripted `incoming/` listing."""

    def __init__(self, ages: dict[str, datetime], page: int = 2) -> None:
        self.ages = dict(ages)
        self.deleted: list[str] = []
        self._page = page

    async def list_incoming(
        self, *, after: str | None, limit: int
    ) -> tuple[Sequence[str], str | None]:
        keys = sorted(self.ages)
        start = keys.index(after) + 1 if after in keys else 0
        window = keys[start : start + min(limit, self._page)]
        nxt = window[-1] if window and start + len(window) < len(keys) else None
        return window, nxt

    async def written_at(self, key: str) -> datetime | None:
        return self.ages.get(key)

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.ages.pop(key, None)


async def _open_session(bed: Bed, session_id: uuid.UUID, state: str) -> None:
    row = FileUploadSession(
        id=session_id,
        org_team_id=bed.org.org_team_id,
        drive_id=bed.drive.id,
        parent_id=bed.drive.root_node_id,
        name=b"pending.bin",
        dedup_domain_id=bed.drive.dedup_domain_id,
        state=state,
        expires_at=NOW + timedelta(days=1),
    )
    bed.session.add(row)
    await bed.session.commit()


async def test_a_live_session_keeps_its_staged_bytes(bed: Bed) -> None:
    """Age alone never releases an object — the owning session decides."""
    live = uuid.uuid4()
    dead = uuid.uuid4()
    await _open_session(bed, live, "open")
    await _open_session(bed, dead, "aborted")
    old = NOW - sweepers.INCOMING_GRACE - SECOND
    admin = FakeIncoming({f"incoming/{live}/part": old, f"incoming/{dead}/part": old}, page=10)

    outcome = await IncomingOrphans(_deps(bed, incoming=admin)).run(NOW)

    assert admin.deleted == [f"incoming/{dead}/part"]
    assert outcome.swept == 1


async def test_the_incoming_sweeper_resumes_from_its_cursor_after_a_kill(bed: Bed) -> None:
    """A killed pass loses no page: the cursor it returned is where it restarts."""
    dead = uuid.uuid4()
    await _open_session(bed, dead, "expired")
    old = NOW - sweepers.INCOMING_GRACE - SECOND
    keys = {f"incoming/{dead}/p{n}": old for n in range(4)}
    admin = FakeIncoming(keys, page=2)
    cp = PausingCheckpoints()

    first = await IncomingOrphans(_deps(bed, incoming=admin, checkpoints=cp)).run(NOW, budget=2)
    assert first.cursor is not None
    assert len(admin.deleted) == 2

    cp.kill("sweepers.incoming_orphans.after_object")
    with pytest.raises(CheckpointKilled):
        await IncomingOrphans(_deps(bed, incoming=admin, checkpoints=cp)).run(
            NOW, cursor=first.cursor, budget=2
        )
    killed = list(admin.deleted)
    assert len(killed) == 3

    cp2 = PausingCheckpoints()
    resumed = await IncomingOrphans(_deps(bed, incoming=admin, checkpoints=cp2)).run(
        NOW, cursor=first.cursor, budget=2
    )
    assert resumed.cursor is None
    assert sorted(admin.deleted) == sorted(keys)


# -- the janitor ------------------------------------------------------------


async def test_the_janitor_runs_every_sweeper_in_the_declared_order(bed: Bed) -> None:
    """The report IS the order — one place to read what ran and what it did."""
    report = await run_janitor(_deps(bed), NOW)

    # Spelled out rather than derived from JANITOR_ORDER: a test that reads the
    # order off the thing it is checking would agree with any reordering.
    assert report.order == (
        "owner_markers",
        "incoming_orphans",
        "deleted_expiry",
        "store_multipart_aborts",
        "expired_sessions",
        "idempotency_keys",
        "content_grant_nonces",
        "page_grant_nonces",
        "operations_retention",
        "history_compaction",
        "conflicts_and_quarantine",
        "unreferenced_acls",
        "trash_purge",
        "operation_watchdog",
        "lease_reaper",
        "live_version_collapse",
        "write_back_collapse",
        "acl_rewrite_drain",
        "acl_rewrite_stale",
        "moving_stale",
        "dir_stats_aggregate",
        "reachability",
    )
    assert len(report.order) == len(set(report.order))
    unwired = {outcome.name for outcome in report.outcomes if outcome.skipped}
    assert unwired == {
        "owner_markers",
        "incoming_orphans",
        "deleted_expiry",
        "store_multipart_aborts",
        "expired_sessions",
        "trash_purge",
        "operation_watchdog",
        "dir_stats_aggregate",
        "reachability",
    }


async def test_a_second_janitor_pass_over_swept_state_clears_nothing(bed: Bed) -> None:
    """Idempotence, stated as the property a schedule depends on."""
    await _plant_idempotency(bed, NOW - IDEMPOTENCY_RETENTION - SECOND)
    await _plant_op(bed, NOW - sweepers.OPERATION_RETENTION - SECOND)
    await _plant_lease(bed, NOW - SECOND)
    await _plant_conflict(bed, NOW - sweepers.RESOLVED_RETENTION - SECOND)

    first = await run_janitor(_deps(bed), NOW)
    assert first.swept == 4

    second = await run_janitor(_deps(bed), NOW, cursors=first.cursors)
    assert second.swept == 0


async def test_the_janitor_refuses_a_budget_that_bounds_nothing(bed: Bed) -> None:
    with pytest.raises(ValueError, match="budget must be >= 1"):
        await run_janitor(_deps(bed), NOW, budget=0)


async def test_a_budgeted_sweeper_says_it_has_more_to_do(bed: Bed) -> None:
    """The cursor is the janitor's signal, not a guess about table size."""
    for _ in range(3):
        await _plant_op(bed, NOW - sweepers.OPERATION_RETENTION - SECOND)

    first = await OperationsRetention(_deps(bed)).run(NOW, budget=2)
    assert (first.swept, first.cursor) == (2, "more")

    second = await OperationsRetention(_deps(bed)).run(NOW, budget=2)
    assert (second.swept, second.cursor) == (1, None)
    assert await _left_ops(bed) == 0


async def test_an_undoable_operation_outlives_its_retention(bed: Bed) -> None:
    """An open undo chain pins the row past ninety days — losing it would make
    the undo button lie."""
    await bed.session.execute(
        text(
            "INSERT INTO file_ops (id, org_team_id, drive_id, kind, actor, state, "
            "progress, errors, conflicts, result, created_at, undoable_until) VALUES "
            "(:id, :org, :drive, 'move', :actor, 'done', '{}'::jsonb, '[]'::jsonb, "
            "'[]'::jsonb, '{}'::jsonb, :at, :until)"
        ),
        {
            "id": uuid.uuid4(),
            "org": bed.org.org_team_id,
            "drive": bed.drive.id,
            "actor": bed.org.admin_id,
            "at": NOW - sweepers.OPERATION_RETENTION - SECOND,
            "until": NOW + timedelta(days=1),
        },
    )
    await bed.session.commit()

    outcome = await OperationsRetention(_deps(bed)).run(NOW)

    assert outcome.swept == 0
    assert await _left_ops(bed) == 1


async def test_a_sweeper_never_reaches_across_orgs(
    bed: Bed, files_session: AsyncSession, files_org_factory: Callable[[], Awaitable[FilesOrg]]
) -> None:
    """The other tenant's expired key is not this janitor's to delete."""
    other = await files_org_factory()
    await files_session.execute(
        text(
            "INSERT INTO file_idempotency_keys (org_team_id, key, principal_id, route, "
            "request_hash, status, body, created_at, expires_at) VALUES "
            "(:org, 'k1', :principal, '/files', 'h', 'succeeded', '{}'::jsonb, :at, :expires)"
        ),
        {
            "org": other.org_team_id,
            "principal": other.admin_id,
            "at": NOW - IDEMPOTENCY_RETENTION - SECOND,
            "expires": NOW - SECOND,
        },
    )
    await files_session.commit()

    outcome = await IdempotencyKeys(_deps(bed)).run(NOW)

    assert outcome.swept == 0
    left = (
        await files_session.execute(
            text("SELECT count(*) FROM file_idempotency_keys WHERE org_team_id = :org"),
            {"org": other.org_team_id},
        )
    ).scalar_one()
    assert left == 1


@pytest.mark.parametrize(
    ("expires", "left"),
    [
        pytest.param(NOW + timedelta(days=5), 1, id="kept-past-a-day-by-its-scope"),
        pytest.param(NOW - SECOND, 0, id="dropped-at-its-own-expiry"),
    ],
)
async def test_a_key_is_swept_on_its_own_expiry_not_on_files_day(
    bed: Bed, expires: datetime, left: int
) -> None:
    """A domain that keeps its keys a week has its keys kept a week, however
    long past Files' day the record was claimed."""
    await bed.session.execute(
        text(
            "INSERT INTO file_idempotency_keys (org_team_id, key, principal_id, route, "
            "request_hash, status, body, created_at, expires_at, scope) VALUES "
            "(:org, 'k1', :principal, 'purchase', 'h', 'succeeded', '{}'::jsonb, :at, "
            ":expires, 'a.week')"
        ),
        {
            "org": bed.org.org_team_id,
            "principal": bed.org.admin_id,
            "at": NOW - timedelta(days=2),
            "expires": expires,
        },
    )
    await bed.session.commit()

    await IdempotencyKeys(_deps(bed)).run(NOW)

    assert await _left_idempotency(bed) == left


async def test_every_ledger_row_that_exists_in_phase_zero_has_a_sweeper() -> None:
    """The order is the ledger: a row added without its sweeper fails here."""
    assert len(JANITOR_ORDER) == 22
    assert all(kind.name for kind in JANITOR_ORDER)


# -- reachability -----------------------------------------------------------

#: Two objects with different content, so their content-addressed keys differ.
REFERENCED = object_key(b"a live version points at these bytes")
ORPHANED = object_key(b"nothing points at these bytes any more")


async def test_a_janitor_pass_moves_an_unreferenced_object_and_keeps_the_rooted_one(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The `objects/` row of the ledger, swept by the pass a deployment runs.

    Both objects are equally old and equally far past the age horizon, so the
    only thing separating them is the version row pointing at one of them: a
    sweep that moved nothing, or moved both, fails.

    The shard number is one no row exists for, which is what a deployment has —
    nothing seeds `file_sweep_shards` — so a sweeper that could only run
    against a hand-inserted row would report itself skipped here.
    """
    seeder, tree = seeded
    long_ago = clock.now() - timedelta(days=3650)
    domain.write(REFERENCED, b"a live version points at these bytes")
    domain.ages.set(domain.absolute(REFERENCED), long_ago)
    await seeder.version(tree["keep.bin"], REFERENCED)
    domain.write(ORPHANED, b"nothing points at these bytes any more")
    domain.ages.set(domain.absolute(ORPHANED), long_ago)

    deps = SweepDeps(
        repo=repo_for_org(files_org.scope),
        ctx=_ctx(files_org),
        reachability=ReachabilityTarget(
            janitor=Janitor(
                repo_for_org, AdminOnlyFactory(domain.store), clock, age_source=domain.ages
            ),
            domain_id=domain.id,
            shard=uuid.uuid4().int % 1_000_000 + 3_000_000,
        ),
    )

    report = await run_janitor(deps, NOW)

    outcome = report.by_name("reachability")
    assert outcome.skipped is False, outcome.reason
    assert outcome.swept == 1
    assert domain.exists(deleted_key(ORPHANED))
    assert not domain.exists(ORPHANED)
    assert domain.exists(REFERENCED)


async def test_a_pass_with_no_reachability_wiring_says_so_instead_of_reporting_zero(
    bed: Bed,
) -> None:
    """An unwired sweep is a visible gap in the report, never a silent zero —
    the whole reason the deployed janitor's missing wiring went unnoticed."""
    report = await run_janitor(_deps(bed), NOW)

    outcome = report.by_name("reachability")
    assert outcome.skipped is True
    assert outcome.swept == 0


# -- the bucket-rooted admin handle, bound to one domain --------------------


def _stage_on_disk(root: Path, domain_id: uuid.UUID, session_id: uuid.UUID, when: datetime) -> Path:
    """One staged object where a bucket-rooted filesystem admin lists it."""
    staged = root / "domains" / str(domain_id) / "incoming" / str(session_id) / "part"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"staged")
    os.utime(staged, (when.timestamp(), when.timestamp()))
    return staged


async def test_a_live_session_keeps_its_bytes_through_a_bucket_rooted_admin(
    bed: Bed, tmp_path: Path
) -> None:
    """The handle a store factory vends is rooted above every domain, so it
    answers `domains/<uuid>/incoming/<session>/…`. The sweeper reads the
    session id out of a domain-RELATIVE key, so an unbound handle makes every
    staged object read session-less and the live-session guard protects
    nothing. Bound to the domain, the live session's bytes survive."""
    live, dead = uuid.uuid4(), uuid.uuid4()
    await _open_session(bed, live, "open")
    await _open_session(bed, dead, "aborted")
    domain_id = bed.drive.dedup_domain_id
    old = NOW - sweepers.INCOMING_GRACE - SECOND
    kept = _stage_on_disk(tmp_path, domain_id, live, old)
    reaped = _stage_on_disk(tmp_path, domain_id, dead, old)
    admin = FilesystemScoped(tmp_path, clock=FakeClock(now=NOW)).admin()

    outcome = await IncomingOrphans(
        _deps(bed, incoming=sweepers.DomainBoundAdmin(admin, DomainId(domain_id)))
    ).run(NOW)

    assert kept.read_bytes() == b"staged"
    assert not reaped.exists()
    assert outcome.swept == 1


async def test_the_bound_admin_never_sweeps_another_domains_staged_bytes(
    bed: Bed, tmp_path: Path
) -> None:
    """One bucket, many domains: the admin lists them all, and an org-scoped
    sweeper may only ever release the bytes of the domain it was bound to —
    a foreign key's session is a row this org's repo cannot even see."""
    domain_id = bed.drive.dedup_domain_id
    stranger = uuid.uuid4()
    old = NOW - sweepers.INCOMING_GRACE - SECOND
    orphan = _stage_on_disk(tmp_path, domain_id, uuid.uuid4(), old)
    foreign = _stage_on_disk(tmp_path, stranger, uuid.uuid4(), old)
    admin = FilesystemScoped(tmp_path, clock=FakeClock(now=NOW)).admin()

    outcome = await IncomingOrphans(
        _deps(bed, incoming=sweepers.DomainBoundAdmin(admin, DomainId(domain_id)))
    ).run(NOW)

    assert not orphan.exists()
    assert foreign.read_bytes() == b"staged"
    assert outcome.swept == 1

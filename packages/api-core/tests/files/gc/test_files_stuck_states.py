"""All six stuck states, planted at once, in one domain.

The reconciliation ledger's promise is not "each sweeper works" — the other
janitor tests already say that one row at a time. It is that a domain which has
been left in *every* broken intermediate state at once is fully described by
`fsck` and fully drained by one janitor pass: six codes out, one pass, none of
the six left. A check that is missing, or a sweeper that is not in
`JANITOR_ORDER`, shows up here as exactly one red row.

The interrupted variant asks the harder half of the same question. The janitor
is killed at the checkpoint between its third and its fourth sweeper and re-run
against the same database; the end state must be identical and each clearing
must have happened *once*, which is read off the outbox rows the clearings emit
rather than off any counter the test itself keeps.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import sweepers
from alkera_core.files.checkpoints import CheckpointKilled, PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import DELETED_WINDOW, Janitor
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.ids import DomainId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.keys import object_key
from alkera_core.files.store.scoped import DomainStore
from alkera_core.files.sweepers import JANITOR_ORDER, SweepDeps, run_janitor
from alkera_core.files.uploads import UploadCompletion
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files.gc.conftest import Domain, MtimeAges, Seeder

fsck_mod = importlib.import_module("alkera_core.files.fsck")

pytestmark = pytest.mark.asyncio

#: The janitor's instant. Well past every deadline in the ledger, so a sweeper
#: that compares against it releases; the deadlines fsck reads live in
#: Postgres `now()`, which is why the plantings below age rows with intervals.
NOW = datetime.now(UTC) + timedelta(days=400)

#: The six codes this test is about, one per stuck state.
STUCK_CODES: tuple[str, ...] = (
    fsck_mod.COMMITTING_STALE,
    fsck_mod.NODE_FLAG_WITHOUT_OP,
    fsck_mod.INCOMING_PAST_TTL,
    fsck_mod.DELETED_PAST_WINDOW,
    fsck_mod.IDEMPOTENCY_KEY_EXPIRED,
)


def _ctx(org: Any) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class StoreIncoming:
    """The janitor's `incoming/` admin handle, backed by the real store.

    Not a fake listing: it lists the same directory tree the plantings wrote
    into and deletes out of it, so "the sweeper cleared it" is observable as
    the bytes being gone from the store the next fsck reads.
    """

    def __init__(self, domain: Domain) -> None:
        self._domain = domain

    async def list_incoming(
        self, *, after: str | None, limit: int
    ) -> tuple[Sequence[str], str | None]:
        keys = sorted(self._domain.listing("incoming"))
        start = keys.index(after) + 1 if after in keys else 0
        window = keys[start : start + limit]
        nxt = window[-1] if window and start + len(window) < len(keys) else None
        return window, nxt

    async def written_at(self, key: str) -> datetime | None:
        return await self._domain.ages.written_at(self._domain.absolute(key))

    async def delete(self, key: str) -> None:
        await self._domain.store.delete(self._domain.absolute(key))


@dataclass
class Planted:
    """The ids of everything planted, so each assertion names its own row."""

    session_id: uuid.UUID
    moving_node: uuid.UUID
    acl_node: uuid.UUID
    incoming_key: str
    deleted_key: str
    idempotency_key: str


@pytest.fixture
async def drive(files_factory: Any) -> Any:
    return await files_factory.drive()


@pytest.fixture
def domain(tmp_path: Path, clock: FakeClock, drive: Any) -> Domain:
    root = tmp_path / "bucket"
    store = FilesystemStore(root, clock=clock, layout="bucket")
    return Domain(id=DomainId(drive.dedup_domain_id), root=root, store=store, ages=MtimeAges(root))


@pytest.fixture
def janitor(repo_for_org: Any, clock: FakeClock, domain: Domain) -> Janitor:
    return Janitor(repo_for_org, AdminOnlyFactory(domain.store), clock, age_source=domain.ages)


@pytest.fixture
def uploads(
    files_session: AsyncSession,
    files_org: Any,
    clock: FakeClock,
    domain: Domain,
) -> UploadCompletion:
    """The real service whose `sweep_expired` the janitor delegates to."""
    domain_root = domain.root / "domains" / str(domain.id)
    store = FilesystemStore(domain_root, clock=clock)
    return UploadCompletion(
        FilesRepo(files_session, OrgScope(org_team_id=files_org.org_team_id)),
        _ctx(files_org),
        clock,
        cast(DomainStore, store),
    )


def _deps(
    files_session: AsyncSession,
    files_org: Any,
    janitor: Janitor,
    domain: Domain,
    uploads: UploadCompletion,
    checkpoints: Any,
) -> SweepDeps:
    return SweepDeps(
        repo=FilesRepo(files_session, files_org.scope),
        ctx=_ctx(files_org),
        checkpoints=checkpoints,
        incoming=StoreIncoming(domain),
        expire_deleted=lambda now: janitor.expire_deleted(domain.id, now),
        sweep_expired_sessions=uploads.sweep_expired,
    )


async def _plant(
    files_session: AsyncSession,
    files_org: Any,
    files_factory: Any,
    drive: Any,
    domain: Domain,
    clock: FakeClock,
) -> Planted:
    """Leave the domain in all six broken intermediate states at once."""
    tree = await files_factory.tree("box/ box/doc.bin moved/ shared/", drive=drive)
    seeder = Seeder(files_session, files_org, drive)

    payload = b"bytes that outlived their reference" * 3
    key = object_key(hash_bytes(payload).content_hash)
    domain.write(key, payload)
    version = await seeder.version(tree["box/doc.bin"], key, size=len(payload))
    tree["box/doc.bin"].head_version_id = version.id
    await files_session.commit()

    # 1. a `committing` session whose worker is gone.
    session_row = await seeder.upload_session(tree["box"], state="committing")
    await files_session.execute(
        text(
            "UPDATE file_upload_sessions SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 hour' WHERE id = :i"
        ),
        {"i": session_row.id},
    )

    # 2 + 3. a node stuck `moving` and a node stuck `acl_rewriting`, neither
    # with a live operation behind it.
    for node, state in ((tree["moved"], "moving"), (tree["shared"], "acl_rewriting")):
        await files_session.execute(
            text(
                "UPDATE file_nodes SET state = :s, updated_at = now() - interval '1 hour' "
                "WHERE id = :i"
            ),
            {"s": state, "i": node.id},
        )

    # 4. staged bytes under `incoming/` whose session row never existed.
    incoming_key = f"incoming/{uuid.uuid4()}/part-1"
    domain.write(incoming_key, b"staged by nobody")
    domain.ages.set(
        domain.absolute(incoming_key), NOW - sweepers.INCOMING_GRACE - timedelta(days=8)
    )

    # 5. an object parked under `deleted/` past the window.
    deleted_key = f"deleted/{key}"
    domain.write(deleted_key, payload)
    # aged against the janitor's clock, which is what fsck compares the write
    # time to; the sweeper's own `now` is later still, so both agree it is past.
    domain.ages.set(domain.absolute(deleted_key), clock.now() - DELETED_WINDOW - timedelta(days=1))

    # 6. a replay record a day and a half old.
    idempotency_key = f"k-{uuid.uuid4().hex[:8]}"
    await files_session.execute(
        text(
            "INSERT INTO file_idempotency_keys (org_team_id, key, principal_id, route, "
            "request_hash, status, body, created_at, expires_at) VALUES "
            "(:org, :key, :principal, '/api/v1/files', 'h', 'succeeded', '{}'::jsonb, "
            "now() - interval '36 hours', now() - interval '12 hours')"
        ),
        {"org": files_org.org_team_id, "key": idempotency_key, "principal": files_org.admin_id},
    )
    await files_session.commit()

    return Planted(
        session_id=session_row.id,
        moving_node=tree["moved"].id,
        acl_node=tree["shared"].id,
        incoming_key=incoming_key,
        deleted_key=deleted_key,
        idempotency_key=idempotency_key,
    )


@pytest.fixture
async def planted(
    files_session: AsyncSession,
    files_org: Any,
    files_factory: Any,
    drive: Any,
    domain: Domain,
    clock: FakeClock,
) -> Planted:
    return await _plant(files_session, files_org, files_factory, drive, domain, clock)


async def _report(janitor: Janitor, files_org: Any, domain: Domain) -> Any:
    return await fsck_mod.run_fsck(janitor, org=files_org.scope, domain_id=domain.id)


def _refs(report: Any, code: str) -> list[str]:
    return [finding.ref_id for finding in report.by_code(code)]


def _details(report: Any, code: str) -> list[dict[str, Any]]:
    return [finding.detail for finding in report.by_code(code)]


# -- fsck sees all six ----------------------------------------------------


@pytest.mark.parametrize(
    "which",
    [
        pytest.param("committing", id="a-committing-session-past-its-deadline"),
        pytest.param("moving", id="a-node-stuck-moving"),
        pytest.param("acl_rewriting", id="a-node-stuck-acl-rewriting"),
        pytest.param("incoming", id="staged-bytes-past-the-session-ttl"),
        pytest.param("deleted", id="a-parked-object-past-the-window"),
        pytest.param("idempotency", id="an-expired-idempotency-key"),
    ],
)
async def test_fsck_reports_every_stuck_state_with_its_own_code(
    janitor: Janitor, files_org: Any, domain: Domain, planted: Planted, which: str
) -> None:
    """Six states, six codes, each naming the row or key that is stuck."""
    report = await _report(janitor, files_org, domain)

    if which == "committing":
        assert _refs(report, fsck_mod.COMMITTING_STALE) == [str(planted.session_id)]
    elif which in ("moving", "acl_rewriting"):
        flagged = report.by_code(fsck_mod.NODE_FLAG_WITHOUT_OP)
        expected = planted.moving_node if which == "moving" else planted.acl_node
        assert [f.ref_id for f in flagged if f.detail["state"] == which] == [str(expected)]
    elif which == "incoming":
        assert _refs(report, fsck_mod.INCOMING_PAST_TTL) == [planted.incoming_key]
    elif which == "deleted":
        assert _refs(report, fsck_mod.DELETED_PAST_WINDOW) == [planted.deleted_key]
    else:
        assert _refs(report, fsck_mod.IDEMPOTENCY_KEY_EXPIRED) == [planted.idempotency_key]
        assert [d["key"] for d in _details(report, fsck_mod.IDEMPOTENCY_KEY_EXPIRED)] == [
            planted.idempotency_key
        ]


# -- one janitor pass clears all six --------------------------------------


async def test_one_janitor_pass_clears_every_stuck_state(
    files_session: AsyncSession,
    files_org: Any,
    janitor: Janitor,
    domain: Domain,
    uploads: UploadCompletion,
    planted: Planted,
) -> None:
    """Every one of the six codes is gone after the janitor, and stays gone."""
    before = await _report(janitor, files_org, domain)
    assert set(STUCK_CODES) <= set(before.codes)

    deps = _deps(files_session, files_org, janitor, domain, uploads, PausingCheckpoints())
    await run_janitor(deps, NOW)

    after = await _report(janitor, files_org, domain)
    assert [code for code in STUCK_CODES if code in after.codes] == []
    # Not "the six are gone" but "fsck has nothing left to say". A
    # sweeper that clears its own row while leaving the domain describable by
    # some *other* check has not finished the job, and the six-code list above
    # would not notice.
    assert after.clean, [(f.code, f.ref_id, f.detail) for f in after.findings]

    # the bytes and the rows, not only the report
    assert not domain.exists(planted.incoming_key)
    assert not domain.exists(planted.deleted_key)
    states = dict(
        (
            await files_session.execute(
                text("SELECT id, state FROM file_nodes WHERE id = ANY(:ids)"),
                {"ids": [planted.moving_node, planted.acl_node]},
            )
        ).all()
    )
    assert set(states.values()) == {"live"}


async def test_a_second_pass_finds_nothing_left_to_sweep(
    files_session: AsyncSession,
    files_org: Any,
    janitor: Janitor,
    domain: Domain,
    uploads: UploadCompletion,
    planted: Planted,
) -> None:
    """Idempotence: the pass after the pass sweeps zero of the six rows."""
    deps = _deps(files_session, files_org, janitor, domain, uploads, PausingCheckpoints())
    await run_janitor(deps, NOW)
    second = await run_janitor(deps, NOW)

    for name in ("incoming_orphans", "deleted_expiry", "idempotency_keys", "expired_sessions"):
        assert second.by_name(name).swept == 0, name
    for name in ("moving_stale", "acl_rewrite_stale"):
        assert second.by_name(name).swept == 0, name


# -- the interrupted pass reaches the same end state ----------------------


async def _node_changed_rows(session: AsyncSession, node_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM event_outbox "
                    "WHERE entity = 'file_node' AND entity_id = :id"
                ),
                {"id": str(node_id)},
            )
        ).scalar_one()
    )


async def test_a_janitor_killed_between_sweepers_resumes_to_the_same_end_state(
    files_session: AsyncSession,
    files_org: Any,
    janitor: Janitor,
    domain: Domain,
    uploads: UploadCompletion,
    planted: Planted,
) -> None:
    """Killed after the third sweeper, re-run: same end state, nothing twice.

    "Nothing twice" is read off the outbox rows the stale-flag clearings emit —
    one `file_node` row per node — because those rows are what a downstream
    consumer would see replayed, and they survive the kill in the database.
    """
    killing = PausingCheckpoints()
    third = JANITOR_ORDER[2].name
    killing.kill(f"janitor.after.{third}")

    with pytest.raises(CheckpointKilled):
        await run_janitor(_deps(files_session, files_org, janitor, domain, uploads, killing), NOW)
    # the first three did their work before the kill; the rest never ran
    assert not domain.exists(planted.incoming_key)
    assert not domain.exists(planted.deleted_key)
    survived = await _report(janitor, files_org, domain)
    assert fsck_mod.IDEMPOTENCY_KEY_EXPIRED in survived.codes
    assert fsck_mod.COMMITTING_STALE in survived.codes
    assert fsck_mod.NODE_FLAG_WITHOUT_OP in survived.codes

    resumed = await run_janitor(
        _deps(files_session, files_org, janitor, domain, uploads, PausingCheckpoints()), NOW
    )

    after = await _report(janitor, files_org, domain)
    assert [code for code in STUCK_CODES if code in after.codes] == []
    # the sweepers that already finished before the kill re-ran and found nothing
    assert resumed.by_name("incoming_orphans").swept == 0
    assert resumed.by_name("deleted_expiry").swept == 0
    # and each flag was cleared exactly once across both runs
    assert await _node_changed_rows(files_session, planted.moving_node) == 1
    assert await _node_changed_rows(files_session, planted.acl_node) == 1

"""The notebook tables against real Postgres: rows round-trip, every check
refuses what it names, the CRDT lane stores notebook documents, and the cell
edit record answers who else edited a cell lately across the 15 s boundary.

Cross-org isolation of the three tables is held by the content row-security
suite (``test_content_row_security.py``), which seeds and polices every table
in ``CONTENT_TABLES``."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.models import CrdtDoc, NotebookEdit, NotebookKernel, NotebookRun
from alkera_core.notebooks.edits import (
    CONCURRENT_EDIT_SECONDS,
    actor_key,
    concurrent_editors,
    edits_since,
    record_edits,
)
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin

pytestmark = [pytest.mark.asyncio]

CELL_A = "a1b2c3d4e5"
CELL_B = "f6g7h8j9k0"


def _kernel(org: uuid.UUID, **overrides: Any) -> NotebookKernel:
    values: dict[str, Any] = {
        "kernel_id": f"k-{uuid.uuid4().hex[:12]}",
        "org_id": org,
        "drive_id": uuid.uuid4(),
        "item_id": uuid.uuid4(),
        "machine_id": "machine-1",
        "state": "idle",
    }
    return NotebookKernel(**{**values, **overrides})


def _run(org: uuid.UUID, **overrides: Any) -> NotebookRun:
    values: dict[str, Any] = {
        "org_id": org,
        "drive_id": uuid.uuid4(),
        "item_id": uuid.uuid4(),
        "actor_kind": "person",
        "actor_display": "Ada",
        "trigger": "run",
        "target": {"kind": "cells", "ids": [CELL_A]},
        "status": "queued",
    }
    return NotebookRun(**{**values, **overrides})


def _edit(org: uuid.UUID, **overrides: Any) -> NotebookEdit:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "org_id": org,
        "item_id": uuid.uuid4(),
        "cell_id": CELL_A,
        "actor_key": "agent:a1",
        "actor_kind": "agent",
        "agent_id": "a1",
        "actor_display": "Agent",
        "first_at": now,
        "last_at": now,
    }
    return NotebookEdit(**{**values, **overrides})


async def test_a_kernel_and_its_run_round_trip(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    kernel = _kernel(org_admin.org_id, seq=7, env_id="default")
    real_session.add(kernel)
    await real_session.flush()
    run = _run(
        org_admin.org_id,
        kernel_id=kernel.kernel_id,
        item_id=kernel.item_id,
        requested_by_user_id=org_admin.admin_id,
        client_run_id="client-run-1",
        frontier="3.AQID",
        frontier_included=True,
        submitted={CELL_A: {"sha256": "0" * 64, "bytes": 12}},
    )
    real_session.add(run)
    await real_session.commit()

    got = (
        await real_session.execute(select(NotebookRun).where(NotebookRun.run_id == run.run_id))
    ).scalar_one()
    assert got.submitted == {CELL_A: {"sha256": "0" * 64, "bytes": 12}}
    assert (got.frontier, got.frontier_included, got.kernel_id) == (
        "3.AQID",
        True,
        kernel.kernel_id,
    )
    assert got.created_at is not None

    # A kernel that goes takes nothing with it but the link.
    await real_session.delete(kernel)
    await real_session.commit()
    await real_session.refresh(got)
    assert got.kernel_id is None


@pytest.mark.parametrize(
    ("make", "constraint"),
    [
        pytest.param(
            lambda o: _kernel(o, state="sleeping"), "ck_notebook_kernels_state", id="k-state"
        ),
        pytest.param(
            lambda o: _kernel(o, seq=-1), "ck_notebook_kernels_seq_nonnegative", id="k-seq"
        ),
        pytest.param(
            lambda o: _run(o, actor_kind="robot"), "ck_notebook_runs_actor_kind", id="r-actor"
        ),
        pytest.param(lambda o: _run(o, trigger="cron"), "ck_notebook_runs_trigger", id="r-trigger"),
        pytest.param(lambda o: _run(o, status="done"), "ck_notebook_runs_status", id="r-status"),
        pytest.param(
            lambda o: _edit(o, cell_id="A1B2C3D4E5"), "ck_notebook_edits_cell_id", id="e-upper"
        ),
        pytest.param(
            lambda o: _edit(o, cell_id="a1b2c3d4eu"), "ck_notebook_edits_cell_id", id="e-u"
        ),
        pytest.param(
            lambda o: _edit(o, cell_id="short"), "ck_notebook_edits_cell_id", id="e-short"
        ),
        pytest.param(
            lambda o: _edit(o, actor_kind="robot"), "ck_notebook_edits_actor_kind", id="e-actor"
        ),
        pytest.param(lambda o: _edit(o, edits=0), "ck_notebook_edits_edits_positive", id="e-edits"),
        pytest.param(
            lambda o: _edit(
                o,
                first_at=datetime.now(UTC),
                last_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
            "ck_notebook_edits_ordered",
            id="e-ordered",
        ),
    ],
)
async def test_every_check_refuses_what_it_names(
    real_session: AsyncSession, org_admin: OrgWithAdmin, make: Any, constraint: str
) -> None:
    real_session.add(make(org_admin.org_id))
    with pytest.raises(IntegrityError) as refused:
        await real_session.flush()
    await real_session.rollback()
    assert constraint in str(refused.value)


async def test_a_client_run_id_is_unique_per_notebook(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    item = uuid.uuid4()
    real_session.add_all(
        [
            _run(org_admin.org_id, item_id=item, client_run_id="same-id-1"),
            # Another notebook, and runs with no client id, are free to repeat.
            _run(org_admin.org_id, client_run_id="same-id-1"),
            _run(org_admin.org_id, item_id=item),
            _run(org_admin.org_id, item_id=item),
        ]
    )
    await real_session.commit()
    real_session.add(_run(org_admin.org_id, item_id=item, client_run_id="same-id-1"))
    with pytest.raises(IntegrityError) as refused:
        await real_session.flush()
    await real_session.rollback()
    assert "uq_notebook_runs_client_run_id" in str(refused.value)


@pytest.mark.parametrize(
    ("doc_type", "admitted"),
    [
        pytest.param("notebook", True, id="notebook"),
        pytest.param("file", True, id="file"),
        pytest.param("chat", False, id="the-op-log-chat"),
    ],
)
async def test_the_crdt_lane_stores_notebook_documents(
    real_session: AsyncSession, org_admin: OrgWithAdmin, doc_type: str, admitted: bool
) -> None:
    real_session.add(
        CrdtDoc(
            org_id=org_admin.org_id,
            doc_type=doc_type,
            doc_id=str(uuid.uuid4()),
            loro_format="1",
            seeded_from="empty",
        )
    )
    if admitted:
        await real_session.flush()
        await real_session.rollback()
        return
    with pytest.raises(IntegrityError) as refused:
        await real_session.flush()
    await real_session.rollback()
    assert "ck_crdt_docs_doc_type" in str(refused.value)


# ---- who edited which cell -----------------------------------------------------


async def _record(
    db: AsyncSession, org: uuid.UUID, item: uuid.UUID, cells: list[str], who: str, at: datetime
) -> None:
    await record_edits(
        db,
        org_id=org,
        item_id=item,
        cell_ids=cells,
        actor_key=who,
        actor_kind="agent" if who.startswith("agent:") else "person",
        user_id=None,
        agent_id=who.removeprefix("agent:") if who.startswith("agent:") else None,
        actor_display=who,
        submit_id="submit-0001",
        at=at,
    )
    await db.commit()


async def test_record_edits_keeps_one_row_per_cell_and_actor(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    org, item = org_admin.org_id, uuid.uuid4()
    t0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    await _record(real_session, org, item, [CELL_A, CELL_B, CELL_A], "agent:a1", t0)
    await _record(real_session, org, item, [CELL_A], "agent:a1", t0 + timedelta(seconds=5))
    # Recorded late, out of order: last_at never moves back, first_at does.
    await _record(real_session, org, item, [CELL_A], "agent:a1", t0 - timedelta(seconds=5))

    rows = {
        r.cell_id: r
        for r in (
            await real_session.execute(select(NotebookEdit).where(NotebookEdit.item_id == item))
        ).scalars()
    }
    assert set(rows) == {CELL_A, CELL_B}
    assert rows[CELL_A].edits == 3
    assert rows[CELL_A].first_at == t0 - timedelta(seconds=5)
    assert rows[CELL_A].last_at == t0 + timedelta(seconds=5)
    assert rows[CELL_B].edits == 1


async def test_concurrent_editors_ages_out_at_the_window(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Driven across the boundary on the wall clock: another actor's edit
    counts for exactly the window, never one's own, never another cell's."""
    org, item = org_admin.org_id, uuid.uuid4()
    me = actor_key(agent_id="me")
    bob = actor_key(user_id=org_admin.admin_id)
    with freeze_time("2026-10-05T12:00:00Z", real_asyncio=True) as frozen:
        await _record(real_session, org, item, [CELL_A], bob, datetime.now(UTC))
        await _record(real_session, org, item, [CELL_A, CELL_B], me, datetime.now(UTC))

        async def seen() -> dict[str, list[str]]:
            found = await concurrent_editors(
                real_session, org_id=org, item_id=item, cell_ids=[CELL_A, CELL_B], actor_key=me
            )
            return {cell: [r.actor_key for r in rows] for cell, rows in found.items()}

        assert await seen() == {CELL_A: [bob]}
        frozen.move_to(
            datetime(2026, 10, 5, 12, 0, tzinfo=UTC) + timedelta(seconds=CONCURRENT_EDIT_SECONDS)
        )
        assert await seen() == {CELL_A: [bob]}, "the window's last instant still counts"
        frozen.tick(timedelta(milliseconds=1))
        assert await seen() == {}


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param({"agent_id": "a1", "user_id": uuid.UUID(int=1)}, "agent:a1", id="agent-first"),
        pytest.param({"user_id": uuid.UUID(int=1)}, f"user:{uuid.UUID(int=1)}", id="person"),
        pytest.param({"machine_id": "m1"}, "machine:m1", id="machine"),
    ],
)
async def test_actor_key_names_the_actor(kwargs: dict[str, Any], expected: str) -> None:
    assert actor_key(**kwargs) == expected


async def test_actor_key_needs_an_actor() -> None:
    with pytest.raises(ValueError, match="person, an agent or a machine"):
        actor_key()


async def test_edits_since_is_the_digest_oldest_first(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    org, item = org_admin.org_id, uuid.uuid4()
    t0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    await _record(real_session, org, item, [CELL_B], "agent:a1", t0 + timedelta(seconds=2))
    await _record(real_session, org, item, [CELL_A], "agent:a2", t0 + timedelta(seconds=1))
    await _record(real_session, org, item, [CELL_A], "agent:old", t0 - timedelta(hours=1))
    found = await edits_since(real_session, org_id=org, item_id=item, since=t0)
    assert [(r.cell_id, r.actor_key) for r in found] == [(CELL_A, "agent:a2"), (CELL_B, "agent:a1")]
    mine_left_out = await edits_since(
        real_session, org_id=org, item_id=item, since=t0, exclude_actor_key="agent:a1"
    )
    assert [r.actor_key for r in mine_left_out] == ["agent:a2"]

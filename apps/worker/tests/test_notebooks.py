"""The notebook run sweep against the test database: every run no engine will
end reaches an ending past its deadline, and each ending is announced on its
notebook's channel. Runs something still holds are left alone."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, Team
from alkera_core.notebooks.models import NotebookKernel, NotebookRun
from alkera_core.notebooks.runs import PLATFORM_EVENTS_KEY, nb_channel
from alkera_core.notebooks.schemas import RUN_FINAL_STATUSES
from sqlalchemy import select
from worker.tasks.notebooks import (
    CONFIRM_WINDOW,
    OPEN_STATUSES,
    RUN_DEADLINE_AFTER,
    STOPPED_GRACE,
    UNANSWERED_AFTER,
    sweep_notebook_runs,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)


async def _org() -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        team = Team(name=f"nb-sweep-{secrets.token_hex(4)}", is_root=True)
        db.add(team)
        await db.commit()
        return team.id


async def _run(
    org_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    status: str,
    created_at: datetime,
    kernel_id: str | None = None,
    started: bool = False,
) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        run = NotebookRun(
            run_id=uuid.uuid4(),
            org_id=org_id,
            drive_id=uuid.uuid4(),
            item_id=item_id,
            actor_kind="person",
            actor_display="Ann",
            trigger="run",
            target={"kind": "all"},
            status=status,
            engine_run_id=f"run-{secrets.token_hex(6)}",
            kernel_id=kernel_id,
            created_at=created_at,
            started_at=created_at if started else None,
        )
        db.add(run)
        await db.commit()
        return run.run_id


async def _kernel(
    org_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    updated_at: datetime,
    stopped_at: datetime | None = None,
    state: str = "busy",
) -> str:
    kernel_id = f"krn_{secrets.token_hex(6)}"
    async with AsyncSessionLocal() as db:
        db.add(
            NotebookKernel(
                kernel_id=kernel_id,
                org_id=org_id,
                drive_id=uuid.uuid4(),
                item_id=item_id,
                machine_id="m-1",
                state="stopped" if stopped_at is not None else state,
                updated_at=updated_at,
                stopped_at=stopped_at,
            )
        )
        await db.commit()
    return kernel_id


async def _status(run_id: uuid.UUID) -> tuple[str, str | None]:
    async with AsyncSessionLocal() as db:
        run = await db.get(NotebookRun, run_id)
        assert run is not None
        return run.status, run.reason


async def _announced(item_id: uuid.UUID) -> list[dict[str, object]]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(EventOutbox).where(EventOutbox.entity_id == nb_channel(item_id))
            )
        ).scalars()
        return [one for row in rows for one in row.payload.get(PLATFORM_EVENTS_KEY, [])]


@pytest.mark.parametrize("status", OPEN_STATUSES)
async def test_every_open_status_ends_by_the_deadline_whatever_else_holds(status: str) -> None:
    """The gate: a status a newer engine adds to the open set is swept too.
    Its kernel is live and heard from, so only the deadline ends it."""
    org, item = await _org(), uuid.uuid4()
    kernel = await _kernel(org, item, updated_at=NOW)
    created = NOW - RUN_DEADLINE_AFTER
    kept = await _run(org, item, status=status, created_at=created + SECOND, kernel_id=kernel)
    over = await _run(org, item, status=status, created_at=created - SECOND, kernel_id=kernel)
    if status != "needs_confirmation":
        await sweep_notebook_runs(NOW)
        assert await _status(kept) == (status, None)
    await sweep_notebook_runs(NOW)
    ended, _ = await _status(over)
    assert ended in RUN_FINAL_STATUSES


async def test_a_run_past_the_deadline_is_interrupted_and_announced() -> None:
    org, item = await _org(), uuid.uuid4()
    kernel = await _kernel(org, item, updated_at=NOW)
    run_id = await _run(
        org,
        item,
        status="running",
        created_at=NOW - RUN_DEADLINE_AFTER - SECOND,
        kernel_id=kernel,
        started=True,
    )
    assert await sweep_notebook_runs(NOW) >= 1
    assert await _status(run_id) == ("interrupted", "run_deadline")
    async with AsyncSessionLocal() as db:
        run = await db.get(NotebookRun, run_id)
        assert run is not None and run.finished_at == NOW
        engine_id = run.engine_run_id
    assert await _announced(item) == [
        {
            "type": "run.finished",
            "run_id": engine_id,
            "status": "interrupted",
            "reason": "run_deadline",
        }
    ]


@pytest.mark.parametrize(
    ("asked", "kernel_heard", "ended"),
    [
        pytest.param(UNANSWERED_AFTER + SECOND, None, True, id="nothing-heard"),
        pytest.param(UNANSWERED_AFTER - SECOND, None, False, id="within-the-window"),
        pytest.param(UNANSWERED_AFTER + SECOND, "after", False, id="a-live-kernel-spoke-since"),
        pytest.param(UNANSWERED_AFTER + SECOND, "before", True, id="the-kernel-spoke-only-before"),
        pytest.param(
            UNANSWERED_AFTER + SECOND, "stopped", True, id="the-kernel-that-spoke-stopped"
        ),
    ],
)
async def test_a_run_nothing_on_the_box_holds_ends_machine_silent(
    asked: timedelta, kernel_heard: str | None, ended: bool
) -> None:
    org, item = await _org(), uuid.uuid4()
    created = NOW - asked
    if kernel_heard == "after":
        await _kernel(org, item, updated_at=created + SECOND)
    elif kernel_heard == "before":
        await _kernel(org, item, updated_at=created - SECOND)
    elif kernel_heard == "stopped":
        await _kernel(org, item, updated_at=created + SECOND, stopped_at=created + SECOND)
    run_id = await _run(org, item, status="queued", created_at=created)
    await sweep_notebook_runs(NOW)
    expected = ("refused", "machine_silent") if ended else ("queued", None)
    assert await _status(run_id) == expected


async def test_another_notebook_s_kernel_does_not_hold_a_run() -> None:
    org, item = await _org(), uuid.uuid4()
    created = NOW - UNANSWERED_AFTER - SECOND
    await _kernel(org, uuid.uuid4(), updated_at=NOW)
    run_id = await _run(org, item, status="queued", created_at=created)
    await sweep_notebook_runs(NOW)
    assert await _status(run_id) == ("refused", "machine_silent")


@pytest.mark.parametrize(
    ("stopped", "ended"),
    [
        pytest.param(STOPPED_GRACE + SECOND, True, id="stopped-past-the-grace"),
        pytest.param(STOPPED_GRACE - SECOND, False, id="its-last-events-may-still-land"),
        pytest.param(None, False, id="still-live"),
    ],
)
async def test_a_run_on_a_stopped_kernel_ends_kernel_restarted(
    stopped: timedelta | None, ended: bool
) -> None:
    org, item = await _org(), uuid.uuid4()
    stopped_at = None if stopped is None else NOW - stopped
    kernel = await _kernel(org, item, updated_at=NOW - timedelta(minutes=5), stopped_at=stopped_at)
    run_id = await _run(
        org,
        item,
        status="running",
        created_at=NOW - timedelta(minutes=10),
        kernel_id=kernel,
        started=True,
    )
    await sweep_notebook_runs(NOW)
    expected = ("kernel_restarted", "kernel_stopped") if ended else ("running", None)
    assert await _status(run_id) == expected


@pytest.mark.parametrize(
    ("asked", "ended"),
    [
        pytest.param(CONFIRM_WINDOW + SECOND, True, id="never-confirmed"),
        pytest.param(CONFIRM_WINDOW - SECOND, False, id="still-waiting"),
    ],
)
async def test_a_run_nobody_confirmed_ends(asked: timedelta, ended: bool) -> None:
    org, item = await _org(), uuid.uuid4()
    await _kernel(org, item, updated_at=NOW)
    run_id = await _run(org, item, status="needs_confirmation", created_at=NOW - asked)
    await sweep_notebook_runs(NOW)
    expected = ("refused", "not_confirmed") if ended else ("needs_confirmation", None)
    assert await _status(run_id) == expected


@pytest.mark.parametrize("status", sorted(RUN_FINAL_STATUSES))
async def test_an_ended_run_is_left_as_it_ended(status: str) -> None:
    org, item = await _org(), uuid.uuid4()
    run_id = await _run(org, item, status=status, created_at=NOW - timedelta(days=3))
    await sweep_notebook_runs(NOW)
    assert await _status(run_id) == (status, None)
    assert await _announced(item) == []

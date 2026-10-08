"""The Files collection workflow through a real Worker on the local dev server.

The workflow carries no collection logic — that is the library's — but it does
carry the two things nothing in-process can show: that the run reaches the
identically named activity with the clock and the dry-run flag intact, and that
the page loop around it walks the bucket to the end instead of stopping at the
first page or spinning on a cursor that stopped moving.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.schemas.temporal import SweepInput
from temporalio.client import Client
from worker.activities import files as activities
from worker.tasks import files as tasks
from worker.workflows.files import FilesGc

pytestmark = pytest.mark.temporal

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


class _Pages:
    """A scripted bucket: each call answers with the next page's outcome."""

    def __init__(
        self, pages: list[tuple[int, str | None]], *, verdicts: list[str] | None = None
    ) -> None:
        self._pages = pages
        self._verdicts = verdicts or ["ok"] * len(pages)
        self.calls: list[tuple[datetime | None, str | None, bool]] = []
        self.overrides: list[bool] = []

    async def __call__(
        self,
        now: datetime | None = None,
        *,
        dry_run: bool = False,
        after: str | None = None,
        **_: Any,
    ) -> tasks.GcPass:
        self.calls.append((now, after, dry_run))
        self.overrides.append(bool(_.get("allow_mass_collect", False)))
        parked, cursor = self._pages[len(self.calls) - 1]
        return tasks.GcPass(
            verdict=self._verdicts[len(self.calls) - 1],
            at=now or NOW,
            orphaned=tuple(str(uuid.uuid4()) for _ in range(1)),
            moved=tuple(f"domains/x/objects/{i}" for i in range(parked)),
            cursor=cursor,
        )


async def test_the_run_reaches_its_activity_with_the_pinned_clock(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replay or a test pins the instant the pass runs against; if the clock
    did not survive the round trip the grace would be measured from now."""
    pages = _Pages([(2, None)])
    monkeypatch.setattr(tasks, "files_gc", pages)

    async with temporal_worker(workflows=[FilesGc], activities=[activities.files_gc]) as running:
        parked = await temporal_client.execute_workflow(
            FilesGc.run,
            args=[SweepInput(now=NOW), False],
            id=f"files-gc-{uuid.uuid4()}",
            task_queue=running.task_queue,
        )

    assert parked.parked == 2
    assert parked.verdict == "ok"
    assert pages.calls == [(NOW, None, False)]


async def test_the_workflow_drives_the_pages_until_the_bucket_runs_short(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound lives in the activity, the completion lives here: each page
    hands back the domain it stopped at and the next starts after it, so a
    bucket larger than one page is still swept whole in one scheduled run."""
    pages = _Pages([(1, "domain-a"), (2, "domain-b"), (3, None)])
    monkeypatch.setattr(tasks, "files_gc", pages)

    async with temporal_worker(workflows=[FilesGc], activities=[activities.files_gc]) as running:
        parked = await temporal_client.execute_workflow(
            FilesGc.run,
            args=[None, False],
            id=f"files-gc-{uuid.uuid4()}",
            task_queue=running.task_queue,
        )

    assert parked.parked == 6
    assert parked.pages == 3
    assert [call[1] for call in pages.calls] == [None, "domain-a", "domain-b"]


async def test_a_cursor_that_stopped_moving_ends_the_run(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page that hands back the cursor it was given would otherwise loop for
    ever, filling a workflow history until the server refuses it. Stopping is
    safe: tomorrow's tick collects again."""
    pages = _Pages([(1, "domain-a"), (1, "domain-a")])
    monkeypatch.setattr(tasks, "files_gc", pages)

    async with temporal_worker(workflows=[FilesGc], activities=[activities.files_gc]) as running:
        parked = await temporal_client.execute_workflow(
            FilesGc.run,
            args=[None, False],
            id=f"files-gc-{uuid.uuid4()}",
            task_queue=running.task_queue,
        )

    assert parked.parked == 2
    assert len(pages.calls) == 2


async def test_a_dry_run_started_by_an_operator_reaches_the_core_as_one(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python -m worker files gc --dry-run`` starts this workflow with the
    flag; a flag dropped anywhere between here and the core would move bytes
    the operator was promised would not move."""
    pages = _Pages([(4, None)])
    monkeypatch.setattr(tasks, "files_gc", pages)

    async with temporal_worker(workflows=[FilesGc], activities=[activities.files_gc]) as running:
        await temporal_client.execute_workflow(
            FilesGc.run,
            args=[None, True],
            id=f"files-gc-{uuid.uuid4()}",
            task_queue=running.task_queue,
        )

    assert pages.calls == [(None, None, True)]


async def test_a_page_that_refuses_ends_the_run_and_the_result_says_why(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal completes the workflow: it is not retried, the pages behind it
    are not visited, and the verdict is what the caller reads."""
    pages = _Pages(
        [(1, "domain-a"), (0, "domain-b"), (4, None)],
        verdicts=["ok", "refused.mass_collect", "ok"],
    )
    monkeypatch.setattr(tasks, "files_gc", pages)

    async with temporal_worker(workflows=[FilesGc], activities=[activities.files_gc]) as running:
        result = await temporal_client.execute_workflow(
            FilesGc.run,
            args=[SweepInput(now=NOW), False],
            id=f"files-gc-{uuid.uuid4()}",
            task_queue=running.task_queue,
        )

    assert result.verdict == "refused.mass_collect"
    assert result.parked == 1
    assert len(pages.calls) == 2


async def test_the_operators_override_reaches_the_job(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = _Pages([(0, None)])
    monkeypatch.setattr(tasks, "files_gc", pages)

    async with temporal_worker(workflows=[FilesGc], activities=[activities.files_gc]) as running:
        await temporal_client.execute_workflow(
            FilesGc.run,
            args=[None, False, True],
            id=f"files-gc-{uuid.uuid4()}",
            task_queue=running.task_queue,
        )

    assert pages.overrides == [True]

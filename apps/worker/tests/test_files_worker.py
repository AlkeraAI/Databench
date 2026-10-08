"""The Files worker family's cores, driven directly.

What these pin is the worker layer's own responsibility, not the library's:
which orgs a janitor pass visits and in which order, that one org's refusal
never costs the rest their sweep, that a job with no wiring refuses rather than
inventing a principal and a bucket, and that the refusals a retry cannot change
leave the activity as a typed non-retryable error. The sweepers' own behaviour
is proven against real rows in ``packages/api-core/tests/files/``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.files.authz.authorize import Denied
from alkera_core.files.errors import InvalidRequest, NotFound, QuotaExceeded
from alkera_core.files.ids import OperationId
from alkera_core.files.store.errors import ChecksumMismatch
from alkera_core.files.sweepers import JANITOR_ORDER, JanitorReport, SweepOutcome
from sqlalchemy.exc import OperationalError
from temporalio.exceptions import ApplicationError
from worker.activities import files as activities
from worker.tasks import files as tasks
from worker.temporal.retry import FILES_NON_RETRYABLE

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class _Recorder:
    """Stands in for the deps factory and for `run_janitor`, and remembers what
    each org's pass was actually handed."""

    def __init__(self, orgs: list[uuid.UUID], fails: set[uuid.UUID] | None = None) -> None:
        self.orgs = orgs
        self.fails = fails or set()
        self.visited: list[uuid.UUID] = []
        self.budgets: list[int] = []

    def deps(self, session: Any, org_team_id: uuid.UUID) -> tasks.FilesJobDeps:
        return tasks.FilesJobDeps(repo=org_team_id, ctx=org_team_id)  # type: ignore[arg-type]

    async def run_janitor(
        self, deps: Any, now: datetime, *, budget: int, cursors: Any = None
    ) -> JanitorReport:
        org = deps.repo
        self.visited.append(org)
        self.budgets.append(budget)
        if org in self.fails:
            raise RuntimeError(f"org {org} refuses")
        return JanitorReport(
            at=now,
            outcomes=tuple(SweepOutcome(name=k.name, swept=1) for k in JANITOR_ORDER),
        )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Every core in this module runs with no real session or Postgres."""

    @asynccontextmanager
    async def _session() -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr(tasks, "_session", _session)
    yield None
    tasks.reset_deps_factory()


def _plant(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    async def _orgs(
        session: Any, *, after: uuid.UUID | None = None, limit: int | None = None
    ) -> list[uuid.UUID]:
        """The paging the SQL does, in memory: ordered by id, after the cursor,
        capped at the page size.

        This stands in for the statement, so nothing below it can prove the
        statement pages correctly — a wrong ORDER BY or a reversed cursor
        comparison would leave every case here green. What the cases below pin
        is the loop AROUND the page: the budget each org's sweep is handed, an
        org whose sweep raised never costing the rest theirs, and the page the
        loop turns into a cursor. The statement itself is pinned against real
        rows in ``test_files_bootstrap.py``
        (``test_the_real_statement_pages_the_fleet_in_id_order_covering_each_org_once``
        and ``test_a_real_janitor_pass_resumes_at_its_cursor_and_sweeps_no_org_twice``).
        """
        page = sorted(recorder.orgs)
        if after is not None:
            page = [org for org in page if org > after]
        return page if limit is None else page[:limit]

    monkeypatch.setattr(tasks, "orgs_with_drives", _orgs)
    monkeypatch.setattr(tasks.sweepers, "run_janitor", recorder.run_janitor)
    tasks.set_deps_factory(recorder.deps)


@pytest.mark.usefixtures("wired")
async def test_the_janitor_sweeps_every_org_that_owns_a_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder([ORG_A, ORG_B])
    _plant(monkeypatch, recorder)

    page = await tasks.janitor(NOW, budget=7)

    assert recorder.visited == [ORG_A, ORG_B]
    assert recorder.budgets == [7, 7]
    assert [r.at for r in page.reports] == [NOW, NOW]
    assert page.cursor is None, "both orgs fit in the default page"


@pytest.mark.usefixtures("wired")
async def test_the_janitor_runs_every_sweeper_in_the_declared_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order is the ledger's order, and the report is what says so."""
    recorder = _Recorder([ORG_A])
    _plant(monkeypatch, recorder)

    [report] = (await tasks.janitor(NOW)).reports

    assert report.order == tuple(kind.name for kind in JANITOR_ORDER)
    # The dir-stats aggregation and the lease reaper ride this pass rather than
    # a schedule of their own, so their absence here would be a silent gap.
    assert {"dir_stats_aggregate", "lease_reaper"} <= set(report.order)
    assert report.swept == len(JANITOR_ORDER)


@pytest.mark.usefixtures("wired")
async def test_one_org_that_refuses_never_costs_the_rest_their_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder([ORG_A, ORG_B], fails={ORG_A})
    _plant(monkeypatch, recorder)

    page = await tasks.janitor(NOW)

    assert recorder.visited == [ORG_A, ORG_B]
    assert len(page.reports) == 1, "the org that raised reports nothing; the other still swept"


@pytest.mark.usefixtures("wired")
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: tasks.promote(OperationId(uuid.uuid4()), ORG_A),
            id="promote",
        ),
        pytest.param(lambda: tasks.acl_rewrite(OperationId(uuid.uuid4()), ORG_A), id="acl-rewrite"),
    ],
)
async def test_an_unwired_job_refuses_rather_than_inventing_a_principal(call: Any) -> None:
    """Which principal a worker acts as, and which bucket it writes to, is a
    deployment decision; with none installed the job must not proceed."""
    tasks.reset_deps_factory()
    with pytest.raises(tasks.FilesJobsNotWiredError):
        await call()


@pytest.mark.usefixtures("wired")
async def test_promote_refuses_when_the_deps_carry_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks.set_deps_factory(lambda session, org: tasks.FilesJobDeps(repo=org, ctx=org))  # type: ignore[arg-type]
    with pytest.raises(tasks.FilesJobsNotWiredError):
        await tasks.promote(OperationId(uuid.uuid4()), ORG_A)


PERMANENT_CASES = [
    pytest.param(ChecksumMismatch("bytes do not match"), "ChecksumMismatch", id="bad-hash"),
    pytest.param(QuotaExceeded(message="over quota"), "QuotaExceeded", id="quota"),
    pytest.param(Denied("files.denied", "not allowed"), "Denied", id="authz"),
    pytest.param(InvalidRequest("no such part"), "InvalidRequest", id="invalid"),
]


@pytest.mark.parametrize(("raised", "type_name"), PERMANENT_CASES)
async def test_a_permanent_refusal_leaves_the_activity_as_a_non_retryable_error(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, type_name: str
) -> None:
    async def _raise(*args: Any, **kwargs: Any) -> str:
        raise raised

    monkeypatch.setattr(tasks, "promote", _raise)
    with pytest.raises(ApplicationError) as caught:
        await activities.files_promote(str(uuid.uuid4()), str(ORG_A))
    assert caught.value.type == type_name
    assert caught.value.non_retryable is True
    assert type_name in FILES_NON_RETRYABLE


async def test_a_transient_failure_stays_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the four refusals are permanent: anything else — a dropped
    connection, a store that is briefly unavailable — must keep its retries."""

    async def _raise(*args: Any, **kwargs: Any) -> str:
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    monkeypatch.setattr(tasks, "promote", _raise)
    with pytest.raises(OperationalError):
        await activities.files_promote(str(uuid.uuid4()), str(ORG_A))


async def test_a_missing_row_is_not_declared_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `NotFound` may be a row this attempt raced, so it is deliberately not
    in the permanent set — the retry budget is what covers it."""

    async def _raise(*args: Any, **kwargs: Any) -> int:
        raise NotFound(message="no such operation")

    monkeypatch.setattr(tasks, "acl_rewrite", _raise)
    with pytest.raises(NotFound):
        await activities.files_acl_rewrite(str(uuid.uuid4()), str(ORG_A))


def test_the_permanent_set_and_the_declared_types_are_the_same_list() -> None:
    """The activity raises by class name and the retry policy refuses by
    string; they are two spellings of one decision, so they must agree."""
    assert tuple(exc.__name__ for exc in activities.PERMANENT) == FILES_NON_RETRYABLE
    assert activities.NON_RETRYABLE_TYPES == FILES_NON_RETRYABLE


ORGS_FIVE = [
    uuid.UUID(f"{n}{n}{n}{n}{n}{n}{n}{n}-{n}{n}{n}{n}-4{n}{n}{n}-8{n}{n}{n}-{n * 12}")
    for n in ("1", "2", "3", "4", "5")
]


@pytest.mark.usefixtures("wired")
async def test_a_pass_sweeps_at_most_its_org_budget_and_names_where_to_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An install with more orgs than one activity can hold is swept a page at
    a time: the pass stops at its budget and hands back the org it stopped on."""
    recorder = _Recorder(list(ORGS_FIVE))
    _plant(monkeypatch, recorder)

    first = await tasks.janitor(NOW, org_budget=2)

    assert recorder.visited == ORGS_FIVE[:2]
    assert first.orgs == tuple(ORGS_FIVE[:2])
    assert first.cursor == ORGS_FIVE[1]
    assert len(first.reports) == 2


@pytest.mark.usefixtures("wired")
async def test_a_pass_resumed_from_a_cursor_starts_at_the_next_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the cursor: the pass a timeout killed does not start
    the fleet again from the first org."""
    recorder = _Recorder(list(ORGS_FIVE))
    _plant(monkeypatch, recorder)

    second = await tasks.janitor(NOW, org_budget=2, after=ORGS_FIVE[1])

    assert recorder.visited == ORGS_FIVE[2:4]
    assert second.cursor == ORGS_FIVE[3]


@pytest.mark.usefixtures("wired")
async def test_the_last_page_of_the_fleet_returns_no_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder(list(ORGS_FIVE))
    _plant(monkeypatch, recorder)

    last = await tasks.janitor(NOW, org_budget=2, after=ORGS_FIVE[3])

    assert recorder.visited == ORGS_FIVE[4:]
    assert last.cursor is None, "a page short of its budget is the end of the fleet"


@pytest.mark.usefixtures("wired")
async def test_an_org_that_refuses_is_still_passed_by_the_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the next page would hand the same refusing org back forever
    and the orgs behind it would never be swept."""
    recorder = _Recorder(list(ORGS_FIVE), fails={ORGS_FIVE[1]})
    _plant(monkeypatch, recorder)

    page = await tasks.janitor(NOW, org_budget=2)

    assert page.orgs == (ORGS_FIVE[0],), "the org that raised swept nothing"
    assert page.cursor == ORGS_FIVE[1], "but the page still moved past it"


@pytest.mark.usefixtures("wired")
@pytest.mark.parametrize("bad", [0, -1])
async def test_a_pass_with_no_org_budget_refuses(monkeypatch: pytest.MonkeyPatch, bad: int) -> None:
    recorder = _Recorder(list(ORGS_FIVE))
    _plant(monkeypatch, recorder)

    with pytest.raises(ValueError, match="org_budget"):
        await tasks.janitor(NOW, org_budget=bad)

    assert recorder.visited == []

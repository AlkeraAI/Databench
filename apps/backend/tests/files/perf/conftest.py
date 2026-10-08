"""The benchmark fixture the perf budget tests take.

The measuring harness itself lives in ``_files_kit`` beside the other shared
helpers: a test module cannot import a ``conftest`` by name without gambling on
which ``conftest.py`` pytest imported first.

What this module adds on top of the harness is the split of a budget row into its
two claims. The statement count holds on any box, so it stays in the default
suite; the p95 is only a proof on a quiet one, so its case is marked
``perf_wallclock`` and the nightly perf job is what selects it. ``report``
therefore asserts one claim per call instead of both, and the ``claim`` fixture
below parametrizes which — it lives here rather than in one budget module so
every budget row in the directory states its budget the same way.

The claim reaches the harness through the ``bench`` fixture rather than only
through ``report``, because it decides how much a row RUNS and not just what it
asserts. The twenty timed repetitions exist to produce a percentile; the
statement half never reads one, so on the gate they were twenty extra requests
per label — over the largest trees in the suite, and on the move row twenty
extra relocations of a ten-thousand-node subtree — whose only product was
discarded. That is what made a statement budget fail as a wall-clock timeout
when the runner was busy.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterator

import pytest
from _files_kit import Bench as _KitBench
from _files_kit import Sample
from _lock_watchdog import LOCK_WATCHDOG_SECONDS, cancel_backends_waiting_on_locks

# One number for the whole suite: the migration harness owns it, since a
# downgrade that rebuilds the pre-0107 index is what a deeper path breaks. The
# purge comes from there too — it is the same doomed set, cleared the same way.
from alkera_core.config import settings
from sqlalchemy import create_engine, text
from tests.migration_harness import DEEPEST_INDEXABLE_PATH, purge_paths_the_old_index_cannot_hold


def purge_deep_file_paths_sync() -> int:
    """Drop every ``file_nodes`` row deeper than :data:`DEEPEST_INDEXABLE_PATH`,
    and answer with the deepest path still in the table.

    The path resolution row builds a chain 256 folders deep, and this suite's
    sessions commit for real with nothing to roll them back — so without this
    the chain outlives the module and belongs to whatever runs next on the
    worker.

    The purge itself is the migration harness's, shared with the downgrade that
    rebuilds the pre-0107 index: a node the real routes wrote is named by its
    history, its versions and its dir-stat deltas, and deleting it without
    those is a foreign-key violation rather than a cleanup.

    A connection of its own, outside the app's async pool: the pool is shared
    with the live backend some rows drive, and disposing it from a teardown
    tore connections out from under in-flight requests, which erred the
    teardown and had the whole row rerun.
    """
    return purge_paths_the_old_index_cannot_hold(DEEPEST_INDEXABLE_PATH).deepest_remaining


def vacuumed_after_the_purge() -> None:
    """Return the table to the next module with its dead rows reclaimed and its
    statistics current: a purge of a hundred-thousand-row tree leaves as many
    dead tuples as it deletes, and a sequential scan over them is what the
    following module's first listing would otherwise pay. VACUUM cannot run
    inside a transaction, so it gets a connection in autocommit."""
    sync_engine = create_engine(settings.database_url_sync, isolation_level="AUTOCOMMIT")
    try:
        with sync_engine.connect() as conn:
            conn.execute(text("VACUUM ANALYZE file_nodes"))
    finally:
        sync_engine.dispose()


async def purge_deep_file_paths() -> int:
    """The same purge from a coroutine, off the event loop thread."""
    return await asyncio.to_thread(purge_deep_file_paths_sync)


@pytest.fixture
def purge_deep_paths() -> Callable[[], Awaitable[int]]:
    """The directory's own cleanup, handed to the test that pins it.

    A fixture rather than an import: a test module cannot import this
    ``conftest`` by name without gambling on which ``conftest.py`` pytest
    imported first, which is the same reason the measuring harness lives in
    ``_files_kit``.
    """
    return purge_deep_file_paths


@pytest.fixture
def deepest_indexable_path() -> int:
    return DEEPEST_INDEXABLE_PATH


@pytest.fixture(autouse=True)
def lock_watchdog() -> Iterator[None]:
    """A row that stops making progress says what it is waiting for.

    Armed around every case in this directory and disarmed the moment one
    finishes, so a row that runs normally never sees it. When it does fire it
    logs the whole of ``pg_stat_activity`` at ERROR — pytest shows that under
    "Captured log call" on the failing test — and cancels whatever is waiting on
    a lock, which turns a worker that sits until the session timeout into a test
    that fails in place with the evidence attached.
    """
    timer = threading.Timer(LOCK_WATCHDOG_SECONDS, cancel_backends_waiting_on_locks)
    # Daemon so an interpreter shutting down between the arm and the cancel is
    # never held open by a timer that has nothing left to watch.
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        # A cancel only stops a timer that has not started running; joining is
        # what keeps a watchdog mid-snapshot from outliving the test that armed
        # it and logging against the next one.
        timer.join(LOCK_WATCHDOG_SECONDS)


@pytest.fixture(autouse=True, scope="module")
def deep_paths_are_not_left_for_the_next_module() -> Iterator[None]:
    """Every module in this directory hands the worker back a shallow tree.

    Once per module rather than after every row: the purge scans the whole
    table, and a teardown that runs seven times over a drive of twenty thousand
    nodes is seven chances to err and have a slow row rerun.
    """
    yield
    deepest = purge_deep_file_paths_sync()
    vacuumed_after_the_purge()
    assert deepest <= DEEPEST_INDEXABLE_PATH, (
        f"a file_nodes path {deepest} labels deep survived this module; the "
        "pre-0107 index a migration downgrade rebuilds cannot be built over it, "
        "so the next migration test on this worker dies half way down"
    )


class Bench(_KitBench):
    """The shared harness, reporting one budget claim at a time."""

    def report(  # type: ignore[override]
        self,
        sample: Sample,
        *,
        claim: str,
        budget_ms: float,
        statements: int,
    ) -> None:
        """Print the row with its measured p95 (the number is printed even on a
        pass) and then assert the half of its budget ``claim`` names."""
        allowed = budget_ms * self.multiplier
        # A row stating a statement count is not timed at all, so it says so
        # rather than printing a latency it never observed.
        latency = (
            f"p95={sample.p95_ms:.1f}ms median={sample.median_ms:.1f}ms "
            f"budget={budget_ms:.0f}ms allowed={allowed:.0f}ms"
            if sample.timed
            else f"untimed budget={budget_ms:.0f}ms"
        )
        print(
            f"\n[files-perf] {sample.label} ({claim}): {latency} "
            f"statements={sample.statements} (max {statements})"
        )
        if claim == "wallclock":
            assert sample.timed, (
                f"{sample.label} asserts a p95 but the row was not timed — the "
                "bench was built for the wrong claim"
            )
            assert sample.p95_ms <= allowed, (
                f"{sample.label} p95 {sample.p95_ms:.1f}ms over {allowed:.0f}ms"
            )
            return
        if claim != "statements":
            raise ValueError(f"unknown budget claim {claim!r}")
        assert sample.statements <= statements, (
            f"{sample.label} used {sample.statements} statements, budget {statements}"
        )


@pytest.fixture(
    params=[
        pytest.param("statements", id="statements"),
        pytest.param("wallclock", id="wallclock", marks=pytest.mark.perf_wallclock),
    ]
)
def claim(request: pytest.FixtureRequest) -> str:
    """Which half of a budget row this case asserts.

    Splitting them is what lets the load-independent half gate every PR while
    the load-sensitive half is deselected until the nightly job asks for it.
    """
    return str(request.param)


@pytest.fixture
def bench(claim: str) -> Bench:
    """The harness, told which claim it is serving.

    It takes ``claim`` rather than being told at ``report`` time because the
    claim decides how much the row *runs*, not just what it asserts: the timed
    repetitions are the load-sensitive part, and a case asserting a statement
    count has no use for them.
    """
    from _fixtures import perf_sizes

    return Bench(perf_sizes().multiplier, claim=claim)


@pytest.fixture
def timed_bench() -> Bench:
    """A harness for a row whose only claim is the latency.

    It does not take ``claim``, so the row stays one case rather than being
    split into a statement half it has nothing to assert in.
    """
    from _fixtures import perf_sizes

    return Bench(perf_sizes().multiplier, claim="wallclock")

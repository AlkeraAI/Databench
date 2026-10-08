"""Which seeds a Files simulation explores, and the options that change them.

A simulation run is a window of consecutive seeds: `sim_seed` is where the
window starts and `sim_seeds` is how wide it is. Two modes decide the start,
and **the default is the deterministic one**:

Pinned — what the PR gate runs
    With no option the window starts at :data:`PINNED_SIM_BASE`, so every
    machine explores the same interleavings in the same order. It used to start
    at a fresh random seed on every run, which made a failure a lottery the
    whole gate played: seed 1303376368 found an interleaving that was red on
    `develop` and on every branch, and whether a PR met it was a coin toss. A
    seed that finds a bug belongs in this window, not in a log.

Fresh (`--sim-random`) — what the nightly soak runs
    A fresh random start per run, which is how new interleavings are found at
    all. The nightly Files workflow passes it, and the seed of a
    failing run is printed below so the failure replays exactly.

`--sim-seed=N` starts the window at N — the line a failure prints — and
`--sim-seeds=K` widens it, which is how the nightly explores more per run.

Whatever the window, every seed in it is its own test case, named by the seed:
a simulation asks for `sim_seed` and :func:`pytest_generate_tests` hands it one
seed per case.
"""

from __future__ import annotations

import os
import random
from collections.abc import AsyncIterator, Callable
from typing import Final

import pytest
from alkera_core.config import settings
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

SIM_SEED_OPTION = "--sim-seed"
SIM_SEEDS_OPTION = "--sim-seeds"
SIM_RANDOM_OPTION = "--sim-random"
#: The argument a simulation takes to receive one seed of the window, and the
#: name under which a case records that seed for the replay line below.
SIM_SEED_PROPERTY = "sim_seed"
#: Where a fresh window start is published for the rest of the run to read.
SIM_BASE_ENV = "ALKERA_SIM_SEED_BASE"
SEED_SPACE = 2**31

#: Where the default window starts. This seed is the one whose interleaving —
#: a sweep that parks tombstones under `deleted/` while four writers commit —
#: went unexplored until it failed a CI attempt at random, so it leads the
#: window rather than sitting in it. The consecutive seeds after it cover all
#: three scheduler policies, which the simulations select with `seed % 3`.
PINNED_SIM_BASE: Final = 1303376368
#: How many seeds one simulation test explores by default.
DEFAULT_SIM_SEEDS: Final = 30


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        SIM_SEED_OPTION,
        action="store",
        default=None,
        type=int,
        help="Start the seed window at this exact seed (what a failure prints to replay).",
    )
    parser.addoption(
        SIM_SEEDS_OPTION,
        action="store",
        default=None,
        type=int,
        help="How many seeds each Files simulation test explores (the nightly raises it).",
    )
    parser.addoption(
        SIM_RANDOM_OPTION,
        action="store_true",
        default=False,
        help="Draw a fresh random seed window instead of the pinned one (the nightly soak).",
    )


def seed_base(*, pinned: int | None, fresh: bool, entropy: Callable[[], int]) -> int:
    """Where this run's seed window starts.

    An explicit seed wins, a fresh run draws one, and everything else — which
    is every default run, so every PR — gets the pinned base.
    """
    if pinned is not None and fresh:
        msg = f"{SIM_SEED_OPTION} and {SIM_RANDOM_OPTION} ask for different seed windows"
        raise pytest.UsageError(msg)
    if pinned is not None:
        return pinned
    return entropy() if fresh else PINNED_SIM_BASE


def _option(config: pytest.Config, name: str, fallback: object) -> object:
    # The options only exist when this directory is an initial conftest (the
    # usual `pytest .../files/sim` invocation); a whole-repo run still gets the
    # defaults rather than a collection error.
    try:
        return config.getoption(name, default=fallback)
    except ValueError:  # pragma: no cover - only on a non-initial conftest load
        return fallback


def fresh_base() -> int:
    """A fresh window start, drawn by the first process to ask and reused by the rest.

    Every process that collects has to agree on which seeds exist: under xdist a
    worker whose window differs from its neighbour's has collected different
    tests, and the run is refused before it starts. The draw is published in the
    environment, which the workers a run spawns inherit.
    """
    published = os.environ.get(SIM_BASE_ENV)
    if published is not None:
        return int(published)
    drawn = random.SystemRandom().randrange(SEED_SPACE)
    os.environ[SIM_BASE_ENV] = str(drawn)
    return drawn


def seed_window(config: pytest.Config) -> list[int]:
    """The seeds this run explores, in order — one test case each."""
    pinned = _option(config, SIM_SEED_OPTION, None)
    width = _option(config, SIM_SEEDS_OPTION, None)
    base = seed_base(
        pinned=None if pinned is None else int(pinned),
        fresh=bool(_option(config, SIM_RANDOM_OPTION, False)),
        entropy=fresh_base,
    )
    return [base + offset for offset in range(DEFAULT_SIM_SEEDS if width is None else int(width))]


def pytest_configure(config: pytest.Config) -> None:
    """Settle this run's window before xdist spawns a worker that would redraw it."""
    seed_window(config)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Give every seed in the window its own case, named by the seed.

    A simulation used to replay the whole window inside one test function, which
    made it a single forty-second item: one worker ran all thirty seeds while
    the others waited on it. The proof is the same either way — the invariant
    holds over these seeds — and a case per seed is handed out a case at a time.
    """
    if SIM_SEED_PROPERTY not in metafunc.fixturenames:
        return
    metafunc.parametrize(SIM_SEED_PROPERTY, seed_window(metafunc.config), ids=str)


@pytest.fixture(autouse=True)
def _replayable_seed(
    request: pytest.FixtureRequest,
    record_property: Callable[[str, object], None],
) -> None:
    """Record a case's seed, so a failure prints the line that replays it."""
    if SIM_SEED_PROPERTY in request.fixturenames:
        record_property(SIM_SEED_PROPERTY, request.getfixturevalue(SIM_SEED_PROPERTY))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Print the seed of a failed simulation run so the replay line is in the log.

    The node id already carries the seed, which is enough to re-run a case of the
    pinned window. A soak's window is drawn rather than pinned, so its failing
    seed only comes back by being named.
    """
    if report.when != "call" or not report.failed:
        return
    for key, value in report.user_properties:
        if key == SIM_SEED_PROPERTY:
            print(f"\n{report.nodeid} ran with {SIM_SEED_OPTION}={value} {SIM_SEEDS_OPTION}=1")


#: The widest lane a simulation runs: the move storm's six movers plus the
#: janitor, with room for the seat a rig opens before the previous case's has
#: finished closing. No overflow, so a lane that quietly grew past what this
#: file accounts for waits on the pool instead of opening a backend nobody
#: counted.
MAX_SIM_SESSIONS = 9

#: Long enough to outlast a checkout queued behind a neighbouring lane on a
#: loaded box, short enough that a rig leaking sessions fails on the pool rather
#: than hanging to the per-test timeout with nothing to read.
SIM_POOL_TIMEOUT_SECONDS = 30.0

#: How long a pooled connection may go unused before it is thrown away and
#: reopened. Age rather than a pre-ping: a seat gives its connection back at the
#: end of every transaction and takes one again for the next, which is close to
#: four thousand checkouts across the two simulations, so a ping on each one
#: would cost more round-trips than the per-case connects it saves.
SIM_POOL_RECYCLE_SECONDS = 300


@pytest.fixture(scope="session")
async def sim_engine() -> AsyncIterator[AsyncEngine]:
    """The one engine every simulation seat and janitor borrows a connection from.

    Each seed used to build its own engine: one TCP connect, one forked Postgres
    backend and one authentication round-trip per actor, thrown away at the end
    of the seed. That churn is not what the simulations are about, and on
    Windows — where a backend is a ``CreateProcess`` — it cost more than the
    interleavings being explored.

    Session-scoped, so the cases that land on one worker share a poolful rather
    than opening one each — they run one after another, and a case closes every
    session it opened before the next one asks for a connection. What a case
    proves is unchanged: a seat still gets its own ``AsyncSession`` on its own
    connection for as long as it holds one, and a connection returning to the
    pool is rolled back, so nothing one case did reaches the next.
    """
    engine = create_async_engine(
        settings.database_url,
        pool_size=MAX_SIM_SESSIONS,
        max_overflow=0,
        pool_timeout=SIM_POOL_TIMEOUT_SECONDS,
        pool_recycle=SIM_POOL_RECYCLE_SECONDS,
    )
    try:
        yield engine
    finally:
        await engine.dispose()

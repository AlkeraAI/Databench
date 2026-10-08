"""The simulation harness proves itself: replayable, and able to find real bugs."""

from __future__ import annotations

import importlib.util
import os
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from actors import (
    assert_single_writer_per_epoch,
    counter_incrementer,
    counter_value,
    lease_holder,
    lease_reaper,
    write_log,
)
from actors_files import ANCIENT, ParkingStore, SweepAges
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import DELETED_WINDOW
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.keys import deleted_key
from scheduler import (
    SIM_EPOCH,
    Partitioned,
    PartitionPolicy,
    PauseLongestPolicy,
    RandomPolicy,
    ReorderCommitsPolicy,
    Sim,
    TraceEntry,
)
from store_model import ModelStore

LEASE_TTL = 5.0
LEASE_SEEDS = range(12)
COUNTER_SEED_BUDGET = 200


def _seed_kit() -> Any:
    """This directory's conftest, loaded by path.

    The test folders have no packages, so importing a conftest by name resolves to
    whichever tree pytest inserted first; loading the sibling by path is the
    same trick the rig uses to borrow the suite's factories.
    """
    source = Path(__file__).resolve().parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("files_sim_seed_kit", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_counters(seed: int, *, locked: bool) -> tuple[Sim, ModelStore]:
    sim = Sim(seed)
    store = ModelStore(sim)
    for name in ("a", "b"):
        sim.spawn(name, counter_incrementer(sim, store, name, locked=locked))
    sim.run()
    return sim, store


def _run_lease(seed: int, *, fenced: bool) -> tuple[Sim, ModelStore]:
    sim = Sim(seed, policy=PauseLongestPolicy(rounds=20, after=8))
    store = ModelStore(sim)
    sim.spawn("a", lease_holder(sim, store, "a", fenced=fenced, ttl=LEASE_TTL, writes=6))
    sim.spawn("b", lease_holder(sim, store, "b", fenced=fenced, ttl=LEASE_TTL, writes=2))
    sim.spawn("reaper", lease_reaper(sim, store, rounds=40))
    sim.run()
    return sim, store


def test_the_same_seed_replays_an_identical_trace() -> None:
    first, _ = _run_counters(4242, locked=False)
    second, _ = _run_counters(4242, locked=False)

    assert first.trace == second.trace
    assert first.trace[0].label == "begin"
    assert first.trace[0] == TraceEntry(first.trace[0].actor, "begin", 0.0)


def test_different_seeds_explore_different_interleavings() -> None:
    traces = {seed: _run_counters(seed, locked=False)[0].trace for seed in range(20)}

    assert len({tuple(t) for t in traces.values()}) > 1


def test_a_lost_update_is_found_within_the_seed_budget(capsys: pytest.CaptureFixture[str]) -> None:
    found: int | None = None
    for seed in range(COUNTER_SEED_BUDGET):
        _, store = _run_counters(seed, locked=False)
        if counter_value(store) != 2:
            found = seed
            break

    assert found is not None, "the unlocked counter never lost an update in 200 seeds"
    print(f"lost update reproduced with --sim-seed={found}")
    assert f"--sim-seed={found}" in capsys.readouterr().out


@pytest.mark.parametrize("seed", range(COUNTER_SEED_BUDGET))
def test_the_locked_counter_never_loses_an_update(seed: int) -> None:
    _, store = _run_counters(seed, locked=True)

    assert counter_value(store) == 2


def test_pause_longest_catches_the_unfenced_holder() -> None:
    failures: dict[int, str] = {}
    for seed in LEASE_SEEDS:
        _, store = _run_lease(seed, fenced=False)
        try:
            assert_single_writer_per_epoch(store)
        except AssertionError as exc:
            failures[seed] = str(exc)

    assert failures, "a stop-the-world pause never exposed the unfenced holder"


@pytest.mark.parametrize("seed", LEASE_SEEDS)
def test_the_fenced_holder_survives_the_pause(seed: int) -> None:
    sim, store = _run_lease(seed, fenced=True)

    with sim.report_on_failure():
        assert_single_writer_per_epoch(store)
        assert write_log(store), "the fenced pair wrote nothing at all"


def test_partition_policy_raises_inside_the_window_only() -> None:
    raised: list[int] = []
    survived: list[int] = []

    async def victim(sim: Sim) -> None:
        for index in range(6):
            try:
                await sim.step("work")
            except Partitioned:
                raised.append(index)
            else:
                survived.append(index)

    sim = Sim(7, policy=PartitionPolicy("victim", 2, 4))
    sim.spawn("victim", victim(sim))
    sim.run()

    assert raised == [2, 3]
    assert survived == [0, 1, 4, 5]


def test_reorder_commits_holds_commits_behind_other_work() -> None:
    async def worker(sim: Sim, store: ModelStore, name: str) -> None:
        tx = store.begin(name)
        for _ in range(5):
            await sim.step("work")
        await tx.write("t", name, {"v": 1})
        await tx.commit()

    sim = Sim(11, policy=ReorderCommitsPolicy(delay=1000))
    store = ModelStore(sim)
    for name in ("a", "b"):
        sim.spawn(name, worker(sim, store, name))
    sim.run()

    labels = [entry.label for entry in sim.trace]
    assert labels == ["work"] * 10 + ["commit"] * 2


def test_a_failing_actor_carries_the_trace_tail_and_the_replay_command() -> None:
    async def doomed(sim: Sim) -> None:
        await sim.step("start")
        msg = "the invariant broke"
        raise AssertionError(msg)

    sim = Sim(99, policy=RandomPolicy())
    sim.spawn("doomed", doomed(sim))

    with pytest.raises(AssertionError) as caught:
        sim.run()

    note = "\n".join(caught.value.__notes__)
    assert "doomed start" in note
    assert "--sim-seed=99" in note
    assert "test_a_failing_actor_carries_the_trace_tail_and_the_replay_command" in note


def test_the_replay_command_names_the_seed_and_this_test() -> None:
    command = Sim(1234).replay_command()

    assert command.startswith("pytest ")
    assert command.endswith("--sim-seed=1234")
    assert "test_the_replay_command_names_the_seed_and_this_test" in command


def test_the_clock_only_moves_when_every_actor_sleeps() -> None:
    observed: list[float] = []

    async def sleeper(sim: Sim) -> None:
        for _ in range(3):
            await sim.step("tick")
            observed.append(sim.time)
            await sim.sleep(2.5)

    sim = Sim(5)
    sim.spawn("sleeper", sleeper(sim))
    sim.run()

    assert observed == [0.0, 2.5, 5.0]
    assert sim.clock.now().minute == 0
    assert sim.clock.now().second == 7


def test_the_default_seed_window_is_the_pinned_one(request: pytest.FixtureRequest) -> None:
    """A default run — every PR — explores the pinned window, not a fresh draw.

    The window this asks for is the one the simulations were parametrized from:
    the same function, given this run's own options.
    """
    kit = _seed_kit()
    if request.config.getoption(kit.SIM_SEED_OPTION) is not None or request.config.getoption(
        kit.SIM_RANDOM_OPTION
    ):
        pytest.skip("this run asked for a seed window of its own")
    window = kit.seed_window(request.config)

    assert window[0] == kit.PINNED_SIM_BASE
    assert len(window) == kit.DEFAULT_SIM_SEEDS
    assert window[-1] == kit.PINNED_SIM_BASE + kit.DEFAULT_SIM_SEEDS - 1
    assert _run_counters(window[0], locked=True)[1] is not None


def test_one_fresh_window_start_is_drawn_for_the_whole_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A soak's processes explore one window, not one window each.

    Under xdist every process collects, and a worker that drew a window of its own
    would have collected tests its neighbours did not — which xdist refuses to
    run. The first draw is published for the rest of the run to read.
    """
    kit = _seed_kit()
    monkeypatch.delenv(kit.SIM_BASE_ENV, raising=False)

    drawn = kit.fresh_base()

    assert os.environ[kit.SIM_BASE_ENV] == str(drawn)
    assert kit.fresh_base() == drawn
    monkeypatch.setenv(kit.SIM_BASE_ENV, str(drawn + 1))
    assert kit.fresh_base() == drawn + 1


def test_an_explicit_seed_starts_the_window_and_a_fresh_run_draws_one() -> None:
    """The three ways a window starts, and the one combination that is a mistake."""
    kit = _seed_kit()
    drawn = [7, 8]

    assert kit.seed_base(pinned=42, fresh=False, entropy=drawn.pop) == 42
    # An explicit seed is a replay, so it wins over nothing and draws nothing.
    assert drawn == [7, 8]
    assert kit.seed_base(pinned=None, fresh=True, entropy=drawn.pop) == 8
    assert kit.seed_base(pinned=None, fresh=False, entropy=drawn.pop) == kit.PINNED_SIM_BASE
    assert drawn == [7]

    with pytest.raises(pytest.UsageError, match="different seed windows"):
        kit.seed_base(pinned=42, fresh=True, entropy=drawn.pop)


async def test_a_tombstone_is_as_old_as_the_move_that_parked_it(tmp_path: Path) -> None:
    """The age the rig reports for a tombstone is the move, not the bytes' birth.

    The sweep only ever *parks* an object under ``deleted/``; the seven-day
    window belongs to ``Janitor.expire_deleted``. A rig that dated a tombstone
    to when its bytes were first written would report every object a sweep
    parks as past that window, which is a complaint about a phase the
    simulation never runs.
    """
    clock = FakeClock(now=SIM_EPOCH)
    inner = FilesystemStore(tmp_path, clock=clock, layout="bucket")
    store = ParkingStore(inner, clock)
    ages = SweepAges(store)
    domain = uuid.uuid4()
    live = f"domains/{domain}/objects/ab/cd/hash"
    parked = f"domains/{domain}/{deleted_key('objects/ab/cd/hash')}"
    source = tmp_path / live
    source.parent.mkdir(parents=True)
    source.write_bytes(b"bytes")

    assert await ages.written_at(live) == ANCIENT

    await store.move(live, parked)

    assert not source.exists(), "the wrapper swallowed the move instead of delegating it"
    assert (tmp_path / parked).read_bytes() == b"bytes"
    assert await ages.written_at(parked) == SIM_EPOCH
    assert await ages.written_at(parked) > clock.now() - DELETED_WINDOW

    clock.advance(DELETED_WINDOW + timedelta(seconds=1))

    # The stamp is the move, so the window really does pass — an age read at
    # query time would keep every tombstone permanently fresh instead.
    assert await ages.written_at(parked) == SIM_EPOCH
    assert await ages.written_at(parked) <= clock.now() - DELETED_WINDOW


def test_an_actor_that_awaits_a_foreign_awaitable_is_refused() -> None:
    async def rogue(sim: Sim) -> None:
        await sim.step("start")

        class _Foreign:
            def __await__(self) -> object:
                yield "not a sim request"

        await _Foreign()  # type: ignore[misc]

    sim = Sim(3)
    sim.spawn("rogue", rogue(sim))

    with pytest.raises(TypeError, match="does not drive"):
        sim.run()

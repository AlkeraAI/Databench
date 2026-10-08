"""The ``--dist loadgroup`` scheduler ``make test-py`` runs with survives a
worker crash (``scripts/xdist_scheduling.py``).

The unit cases drive the scheduler the way xdist's controller does — nodes are
added, collections registered, tests marked complete as a worker would report
them — through a small model of the worker protocol whose one important detail
is the real one: a worker fetches the command *after* the item it holds before
it runs that item, so a worker holding one item and no further command is
parked. The end-to-end case runs a real xdist session in a subprocess with a
worker that dies mid-run and asserts the run finishes and reports.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from xdist.scheduler.loadgroup import LoadGroupScheduling
from xdist_scheduling import (
    DIST_MODE,
    CrashSafeLoadGroupScheduling,
    scheduler_for,
)

pytest_plugins = ["pytester"]

SCHEDULER_MODULE = Path(__file__).resolve().parents[3] / "scripts" / "xdist_scheduling.py"


class Terminal:
    """The terminal reporter as the scheduler uses it: a sink for lines."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_line(self, line: str, **_markup: Any) -> None:
        self.lines.append(line)


class Config:
    """What ``LoadScopeScheduling`` reads off ``pytest.Config``: the ``--tx``
    specs (one per worker), ``--dist``, the reorder option, and the plugin
    manager the scheduler asks for the terminal."""

    def __init__(self, workers: int = 2, dist: str = DIST_MODE, terminal: Terminal | None = None):
        self._values = {"tx": [f"{workers}*popen"], "dist": dist}
        self.option = SimpleNamespace(loadscopereorder=False)
        self.pluginmanager = SimpleNamespace(
            getplugin=lambda name: terminal if name == "terminalreporter" else None
        )
        self.hook = SimpleNamespace(pytest_collectreport=lambda report: None)

    def getvalue(self, name: str) -> Any:
        return self._values[name]


class Node:
    """A worker as the controller sees it, plus the queue the real worker keeps.

    ``held`` is the worker's own queue of item indices in arrival order, with
    ``True`` standing for the shutdown marker. The worker runs its head item
    only once the command *after* it has arrived — another index or the
    shutdown — exactly like ``xdist.remote.WorkerInteractor.run_one_test``.
    """

    def __init__(self, name: str) -> None:
        self.gateway = SimpleNamespace(id=name)
        self.held: list[int | bool] = []
        self.commands: list[list[int]] = []
        self._shutdown = False

    @property
    def shutting_down(self) -> bool:
        return self._shutdown

    def shutdown(self) -> None:
        if not self._shutdown:
            self.held.append(True)
        self._shutdown = True

    def send_runtest_some(self, indices: Sequence[int]) -> None:
        self.commands.append(list(indices))
        self.held.extend(indices)

    @property
    def can_run(self) -> bool:
        return len(self.held) >= 2 and self.held[0] is not True

    @property
    def parked(self) -> bool:
        """Holding an item and waiting for a command that has not come."""
        return len(self.held) == 1 and self.held[0] is not True

    @property
    def finished(self) -> bool:
        return self.held == [True]

    def run_one(self, sched: LoadGroupScheduling) -> str:
        index = self.held.pop(0)
        assert isinstance(index, int)
        nodeid = sched.registered_collections[self][index]
        sched.mark_test_complete(self, index)
        return nodeid


def collection(groups: int, per_group: int = 2) -> list[str]:
    return [f"m{g}.py::t{i}@g{g}" for g in range(1, groups + 1) for i in range(per_group)]


def start(sched: LoadGroupScheduling, nodes: Sequence[Node], ids: Sequence[str]) -> None:
    """Bring ``nodes`` up on ``ids`` the way the controller does before the first test."""
    for node in nodes:
        sched.add_node(node)
    for node in nodes:
        sched.add_node_collection(node, list(ids))
    assert sched.collection_is_completed
    sched.schedule()


def tick(sched: LoadGroupScheduling) -> None:
    """What ``DSession.loop_once`` does after every event: once the queue is
    empty and no worker holds two items, every worker is told to shut down —
    the command that lets each run the last item it holds."""
    if sched.tests_finished:
        for node in sched.nodes:
            node.shutdown()


def run_until_quiet(sched: LoadGroupScheduling, nodes: Sequence[Node]) -> list[str]:
    """Let every worker run whatever it can, round-robin, with the controller's
    tick after each report, until nothing can move."""
    ran: list[str] = []
    tick(sched)
    progressed = True
    while progressed:
        progressed = False
        for node in nodes:
            if node in sched.assigned_work and node.can_run:
                ran.append(node.run_one(sched))
                tick(sched)
                progressed = True
    return ran


#: The tail of a real run: units of one test each (a ``spread`` module's), more
#: of them than two workers hold at once.
TAIL = collection(groups=6, per_group=1)


def crash_after_finished_units(sched: LoadGroupScheduling) -> tuple[Node, Node, str, list[str]]:
    """Two workers on :data:`TAIL`; the first finishes two units, then dies
    holding two more, while the other has not moved yet.

    Returns the two nodes, the item the crash was charged to, and what ran
    before it. The dying worker's ledger then holds more finished units than
    the survivor holds items — the shape of every crash late in a run, and the
    one in which requeueing the finished units parks the survivor.
    """
    a, b = Node("gw0"), Node("gw1")
    start(sched, [a, b], TAIL)
    ran = [a.run_one(sched), a.run_one(sched)]
    assert len(ran) == 2 and a.held and not a.finished, "the dying worker is holding more work"
    assert b.commands and not b.finished, "the survivor holds work of its own"
    crashitem = sched.remove_node(a)
    assert crashitem is not None
    tick(sched)
    return a, b, crashitem, ran


def test_after_a_crash_the_survivor_runs_everything_left_and_is_never_sent_an_empty_command():
    sched = CrashSafeLoadGroupScheduling(Config())  # type: ignore[arg-type]
    _a, b, crashitem, before = crash_after_finished_units(sched)

    after = run_until_quiet(sched, [b])

    assert not b.parked, f"the survivor is parked holding {b.held}"
    assert b.finished, f"the survivor was never told to shut down; it holds {b.held}"
    assert [] not in b.commands, "a command naming no test parks the worker that gets it"
    assert set(before) | set(after) == set(TAIL), "a test the crashed worker never ran was lost"
    assert crashitem in after, "the item the crash was charged to is run again elsewhere"
    assert not (set(before) & set(after)), "a test the crashed worker finished is not run twice"


def test_the_crash_is_charged_to_the_first_item_the_dead_worker_never_finished():
    sched = CrashSafeLoadGroupScheduling(Config())  # type: ignore[arg-type]
    a, _b, crashitem, _before = crash_after_finished_units(sched)

    held = a.held[0]
    assert isinstance(held, int)
    assert sched.collection is not None
    assert crashitem == sched.collection[held]


def test_the_stock_scheduler_parks_the_survivor_after_the_same_crash():
    """The bug this module works around, pinned in xdist's own class.

    The day this case fails, xdist ships a ``remove_node`` that requeues only
    unfinished units, and ``CrashSafeLoadGroupScheduling`` can retire.
    """
    sched = LoadGroupScheduling(Config())  # type: ignore[arg-type]
    _a, b, _crashitem, _before = crash_after_finished_units(sched)

    run_until_quiet(sched, [b])

    assert [] in b.commands
    assert b.parked, "xdist now keeps a worker busy after a crash; drop the subclass"
    assert sched.workqueue, "the tests the dead worker never ran are still queued"


def test_a_worker_that_finished_everything_leaves_without_a_crash_item_or_a_requeue():
    sched = CrashSafeLoadGroupScheduling(Config())  # type: ignore[arg-type]
    a, b = Node("gw0"), Node("gw1")
    start(sched, [a, b], collection(groups=2))
    run_until_quiet(sched, [a, b])
    assert a.finished and b.finished

    assert sched.remove_node(a) is None
    assert not sched.workqueue
    assert b.finished and [] not in b.commands


def test_a_crash_after_the_others_were_sent_home_leaves_its_work_to_the_replacement():
    """Three workers, four units: two are told to shut down at once, the third
    finishes a unit and dies holding the last one. The shutdown the others
    already have still lets them run what they hold, and the unit the dead
    worker left goes to the replacement the controller spawns — to nobody
    else, and nothing is lost."""
    sched = CrashSafeLoadGroupScheduling(Config(workers=3))  # type: ignore[arg-type]
    nodes = [Node("gw0"), Node("gw1"), Node("gw2")]
    ids = collection(groups=4)
    start(sched, nodes, ids)
    first = nodes[0]
    assert all(node.shutting_down for node in nodes[1:]), "two workers were sent home at once"
    before = [first.run_one(sched), first.run_one(sched)]
    sched.remove_node(first)
    tick(sched)
    replacement = Node("gw3")
    sched.add_node(replacement)
    sched.add_node_collection(replacement, list(ids))
    sched.schedule()

    ran = run_until_quiet(sched, [*nodes[1:], replacement])

    survivors = [*nodes[1:], replacement]
    assert all(node.finished for node in survivors), [node.held for node in survivors]
    assert [] not in [command for node in survivors for command in node.commands]
    assert set(before) | set(ran) == set(ids)
    assert len(before) + len(ran) == len(ids), "every test ran exactly once"
    assert sched.collection is not None
    assert {sched.collection[i] for i in replacement.commands[0]} == set(ids[-2:])


def test_two_crashes_in_a_row_wait_for_each_replacement_to_collect_before_scheduling_it():
    """Both original workers die, the second while the first one's replacement
    is still collecting, and the second replacement comes up before the first
    has collected. Every reschedule in between (the second crash, then the
    first replacement's collection) leaves a node with no registered
    collection alone; each replacement is served once its own collection
    lands, and every test runs. The ``KeyError: <WorkerController gw3>`` this
    pins came from scheduling the second replacement on the first one's
    collection (pr-gate run 37112513052, Windows)."""
    sched = CrashSafeLoadGroupScheduling(Config())  # type: ignore[arg-type]
    _a, b, _crashitem, before = crash_after_finished_units(sched)
    first = Node("gw2")
    sched.add_node(first)

    assert sched.remove_node(b) is not None, "the survivor died holding work"
    assert first.commands == [], "a node is not served before its collection is registered"
    second = Node("gw3")
    sched.add_node(second)
    sched.add_node_collection(first, list(TAIL))
    sched.schedule()
    assert second.commands == [] and not second.shutting_down
    sched.add_node_collection(second, list(TAIL))
    sched.schedule()

    after = run_until_quiet(sched, [first, second])

    assert first.commands and second.commands, "both replacements were given work"
    assert first.finished and second.finished, [first.held, second.held]
    assert [] not in first.commands + second.commands
    assert set(before) | set(after) == set(TAIL), "a test neither dead worker finished was lost"
    assert len(before) + len(after) == len(TAIL), "every test ran exactly once"


def test_a_replacement_given_single_test_units_holds_two_and_is_never_parked():
    """The other worker finished and went home; the crashed one left two
    one-test units. The replacement must be handed both: holding only the
    first, it waits for a command that never comes (no test completes to
    trigger a top-up, and a unit still queued keeps the run from sending
    anyone home), and the session never ends."""
    sched = CrashSafeLoadGroupScheduling(Config())  # type: ignore[arg-type]
    a, b = Node("gw0"), Node("gw1")
    start(sched, [a, b], TAIL)
    run_until_quiet(sched, [b])
    assert b.finished, "the other worker ran out of work and was sent home"
    assert sched.remove_node(b) is None
    assert sched.collection is not None
    left = {sched.collection[i] for i in a.held if isinstance(i, int)}
    assert len(left) == 2, "the dying worker holds two one-test units"
    assert sched.remove_node(a) is not None
    replacement = Node("gw2")
    sched.add_node(replacement)
    sched.add_node_collection(replacement, list(TAIL))
    sched.schedule()

    ran = run_until_quiet(sched, [replacement])

    assert not replacement.parked, f"the replacement is parked holding {replacement.held}"
    assert replacement.finished
    assert set(ran) == left


# -- a replacement worker's collection ------------------------------------------------


def with_a_replacement(
    replacement_ids: Sequence[str], *, terminal: Terminal
) -> tuple[CrashSafeLoadGroupScheduling, Node, Node]:
    """A run whose first worker died after finishing its first unit, and a
    replacement that arrives having collected ``replacement_ids``."""
    sched = CrashSafeLoadGroupScheduling(Config(terminal=terminal))  # type: ignore[arg-type]
    _a, b, _crashitem, _before = crash_after_finished_units(sched)
    replacement = Node("gw2")
    sched.add_node(replacement)
    sched.add_node_collection(replacement, list(replacement_ids))
    sched.schedule()
    tick(sched)
    return sched, b, replacement


def test_a_replacement_that_collected_the_same_tests_in_another_order_serves_them():
    terminal = Terminal()
    sched, b, replacement = with_a_replacement(list(reversed(TAIL)), terminal=terminal)

    ran = run_until_quiet(sched, [b, replacement])

    assert replacement.commands, "the replacement was given work"
    assert replacement.finished and b.finished
    # Indices were resolved against the replacement's OWN order: what it ran is
    # a real test, and every test ran once across the survivors.
    assert set(ran) | set(TAIL[:4:2]) == set(TAIL)
    assert len(ran) == len(TAIL) - 2
    assert any("another order" in line for line in terminal.lines)


def test_a_replacement_that_collected_a_different_set_is_named_and_sent_away():
    terminal = Terminal()
    sched, b, replacement = with_a_replacement([*TAIL[:-1], "m9.py::t0@g9"], terminal=terminal)

    ran = run_until_quiet(sched, [b, replacement])

    assert replacement.shutting_down
    assert replacement.commands == [], "a node whose tests are not the run's runs none"
    assert b.finished, "the run goes on with the workers that agree"
    assert set(ran) | set(TAIL[:4:2]) == set(TAIL)
    said = "\n".join(terminal.lines)
    assert "Different tests were collected between gw0 and gw2" in said
    assert "+m9.py::t0@g9" in said, "the difference itself is on the terminal"
    assert "worker gw2 collected a different set of tests" in said
    assert "sent away" in said


def test_a_replacement_that_collected_identically_is_registered_like_any_worker():
    terminal = Terminal()
    sched, b, replacement = with_a_replacement(TAIL, terminal=terminal)

    ran = run_until_quiet(sched, [b, replacement])

    assert replacement.finished and b.finished
    assert set(ran) | set(TAIL[:4:2]) == set(TAIL)
    assert terminal.lines == []


# -- the factory ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dist", "served"),
    [
        pytest.param("loadgroup", True, id="loadgroup"),
        pytest.param("loadscope", False, id="loadscope"),
        pytest.param("load", False, id="load"),
        pytest.param("each", False, id="each"),
    ],
)
def test_only_the_mode_make_runs_is_served(dist: str, served: bool):
    sched = scheduler_for(Config(dist=dist))  # type: ignore[arg-type]
    assert isinstance(sched, CrashSafeLoadGroupScheduling) is served
    if not served:
        assert sched is None


# -- the real thing ---------------------------------------------------------------------


def test_a_run_whose_worker_dies_mid_way_finishes_and_reports(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workers, seven one-test groups, and the last test kills the worker
    running it once that worker has finished something else — the shape of a
    crash late in a real run. The stock scheduler then hands a requeued
    finished unit to the replacement as an empty command and the session never
    ends; with the crash-safe one it ends, the crash is reported, and the test
    the crash interrupted runs again on the replacement."""
    for var in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        monkeypatch.delenv(var, raising=False)
    pytester.makeconftest(
        textwrap.dedent(
            f"""
            import importlib.util, sys
            import pytest

            path = {str(SCHEDULER_MODULE)!r}
            spec = importlib.util.spec_from_file_location("xdist_scheduling", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["xdist_scheduling"] = module
            spec.loader.exec_module(module)

            @pytest.hookimpl(optionalhook=True)
            def pytest_xdist_make_scheduler(config, log):
                return module.scheduler_for(config, log)
            """
        )
    )
    for group in range(1, 7):
        pytester.makepyfile(
            **{
                f"test_g{group}": textwrap.dedent(
                    f"""
                    import os, pathlib, pytest
                    pytestmark = pytest.mark.xdist_group("g{group}")

                    def test_one():
                        worker = os.environ["PYTEST_XDIST_WORKER"]
                        pathlib.Path(worker + ".ran").write_text("x")
                    """
                )
            }
        )
    pytester.makepyfile(
        test_g7=textwrap.dedent(
            """
            import os, pathlib, pytest
            pytestmark = pytest.mark.xdist_group("g7")

            def test_the_one_that_kills_its_worker():
                worker = os.environ["PYTEST_XDIST_WORKER"]
                # Only a worker of the original pair that has finished a test
                # dies here; a replacement runs this test to completion.
                if worker in ("gw0", "gw1") and pathlib.Path(worker + ".ran").exists():
                    os._exit(1)
            """
        )
    )

    result = pytester.runpytest_subprocess(
        "-n", "2", "--dist", "loadgroup", "-p", "no:cacheprovider", "-p", "xdist", timeout=90
    )

    outcomes = result.parseoutcomes()
    assert outcomes.get("passed") == 7, result.outlines[-15:]
    assert outcomes.get("failed", 0) >= 1, "the crash itself is reported as a failure"
    result.stdout.fnmatch_lines(
        ["*crashed while running*test_g7.py::test_the_one_that_kills_its_worker*"]
    )
    assert not any("INTERNALERROR" in line for line in result.outlines + result.errlines)
    assert result.ret == pytest.ExitCode.TESTS_FAILED

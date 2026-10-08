"""The scheduler ``make test-py`` distributes with: xdist's group scheduling,
kept alive across a worker crash.

``--dist loadgroup`` hands each worker whole units of work (one ``xdist_group``
each) and keeps a per-worker ledger of every unit it was ever given, marking
tests done as the worker reports them. Three things go wrong in that ledger when
a worker dies mid-run, and the repo-root ``conftest.py`` installs this subclass
through ``pytest_xdist_make_scheduler`` to close all three:

* **Finished units come back.** A unit is never taken off a worker's ledger
  once its last test is done, so when the worker crashes the stock
  ``remove_node`` puts *every* unit it ever held back on the queue, the finished
  ones included. Requeued units sit behind the real work, so near the end of
  the run each free worker pops one, is sent a ``runtests`` command naming no
  test at all, and waits for a command that never comes, a worker fetches the
  command *after* the item it holds before it runs that item, so the item it
  already holds never runs either. With enough finished units requeued every
  worker parks that way and the run hangs at 99 % with nothing executing:
  pytest-timeout has no test to interrupt, and the item a parked worker held
  is what xdist later names as "crashed while running". A crashed worker's
  units go back only when they still have a test to run, and a worker is never
  sent an empty command: with nothing left it is told to shut down, which is
  what makes it run the item it holds and end.

* **A replacement that collected differently is silently unusable.** The
  worker spawned in a crashed one's place collects on its own, and when its
  list of test ids is not identical to the run's the stock scheduler logs the
  difference at debug level and never registers it, then schedules the node
  anyway and dies with ``KeyError: <WorkerController gwN>`` inside its own
  bookkeeping, an INTERNALERROR that skips the failure listing. Here a
  replacement that collected the same tests in another order is registered
  (each worker is addressed by indices into its own list, so order is
  harmless), and one that collected a different set is named on the terminal,
  together with the difference, and sent away; the run goes on with the
  workers that agree and reports as usual.

* **A replacement is scheduled before it has collected.** When two workers
  die close together, the second replacement is added to the run while the
  first is still collecting; the first one's collection then reschedules every
  node, the second included, and indexing its missing collection raised the
  same ``KeyError``. A node is topped up only once its collection is
  registered.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import pytest
from xdist.report import report_collection_diff
from xdist.scheduler.loadgroup import LoadGroupScheduling

if TYPE_CHECKING:
    from xdist.workermanage import WorkerController

#: The ``--dist`` mode this scheduler serves; every other mode is xdist's own.
DIST_MODE = "loadgroup"


class CrashSafeLoadGroupScheduling(LoadGroupScheduling):
    """xdist's ``loadgroup`` scheduling, without the two failure modes a
    crashed worker triggers (see the module docstring)."""

    def __init__(self, config: pytest.Config, log: Any | None = None) -> None:
        super().__init__(config, log)
        self._crash_safe_config = config

    def remove_node(self, node: WorkerController) -> str | None:
        """Take ``node`` out of the run; requeue only the units it left unfinished.

        Returns the test the node was executing when it died (the first one it
        never reported), or ``None`` when it had finished everything, a worker
        shutting down normally comes through here too.
        """
        workload = self.assigned_work.pop(node)
        unfinished: OrderedDict[str, dict[str, bool]] = OrderedDict(
            (scope, unit) for scope, unit in workload.items() if not all(unit.values())
        )
        if not unfinished:
            return None
        crashitem = next(
            nodeid for unit in unfinished.values() for nodeid, done in unit.items() if not done
        )
        self.workqueue.update(unfinished)
        for other in list(self.assigned_work):
            self._reschedule(other)
        return crashitem

    def _reschedule(self, node: WorkerController) -> None:
        """Top ``node`` up, unless it has not reported its collection yet.

        A replacement worker is added to the run when it comes up and registers
        its collection only once it has collected. Anything that reschedules
        every node in between (another worker crashing, or the first
        replacement finishing its collection) would otherwise index a
        collection that is not there yet. The node is scheduled by the
        ``schedule()`` its own collection triggers.

        After the stock top-up, a node still holding fewer than two tests is
        given more while the queue has any. A worker runs the item it holds
        only once the command after it arrives, and the next top-up comes only
        when it reports a test done, so a node left holding one test while
        work is still queued is parked for good: the run never reaches the
        point where every node is sent home. That is where a replacement lands
        when the units requeued from a crash are single tests.
        """
        if node not in self.registered_collections:
            return
        super()._reschedule(node)
        while (
            self.workqueue
            and not node.shutting_down
            and self._pending_of(self.assigned_work[node]) < 2
        ):
            self._assign_work_unit(node)

    def _assign_work_unit(self, node: WorkerController) -> None:
        """Send ``node`` the next unit with a test still to run.

        A unit with nothing left in it is dropped rather than sent: a command
        that names no test leaves the worker waiting forever. With no such unit
        on the queue the node is told to shut down instead, exactly as
        ``_reschedule`` does when the queue is empty, the shutdown queues behind
        the items the worker already holds, so those still run.
        """
        worker_collection = self.registered_collections[node]
        while self.workqueue:
            scope, work_unit = self.workqueue.popitem(last=False)
            indices = [
                worker_collection.index(nodeid) for nodeid, done in work_unit.items() if not done
            ]
            if not indices:
                continue
            self.assigned_work.setdefault(node, OrderedDict())[scope] = work_unit
            node.send_runtest_some(indices)
            return
        node.shutdown()

    def add_node_collection(self, node: WorkerController, collection: Sequence[str]) -> None:
        """Register what ``node`` collected.

        Before the first schedule every node's list is registered and compared
        as xdist does. A node that arrives afterwards, the replacement for a
        crashed one, is registered when it collected the run's tests in any
        order, and sent away with the difference on the terminal when it did
        not; ``_reschedule`` skips a node that is shutting down, so nothing
        indexes a collection it never registered.
        """
        if not self.collection_is_completed or self.collection is None:
            super().add_node_collection(node, collection)
            return
        collected = list(collection)
        if collected == self.collection:
            super().add_node_collection(node, collection)
            return
        assert node in self.assigned_work, "add_node() registers a node before its collection"
        worker = node.gateway.id
        if sorted(collected) == sorted(self.collection):
            self.registered_collections[node] = collected
            self._say(
                f"worker {worker} collected the run's tests in another order; it serves them "
                "by its own indices"
            )
            return
        official = next(iter(self.registered_collections))
        self._say(
            report_collection_diff(self.collection, collected, official.gateway.id, worker)
            + f"\nworker {worker} collected a different set of tests than the run is scheduling; "
            "it is sent away and the run goes on with the workers that agree"
        )
        node.shutdown()

    def _say(self, message: str) -> None:
        """Put ``message`` in front of the person watching the run, not only in
        the debug log nobody has on."""
        self.log(message)
        terminal = self._crash_safe_config.pluginmanager.getplugin("terminalreporter")
        if terminal is not None:
            terminal.write_line("")
            terminal.write_line(message, red=True)


def scheduler_for(config: pytest.Config, log: Any | None = None) -> Any | None:
    """The scheduler for this run, or ``None`` to leave the choice to xdist.

    Only ``--dist loadgroup`` is served: it is the mode ``make test-py`` runs,
    and the ledger this class corrects is the one that mode keeps.
    """
    if config.getvalue("dist") != DIST_MODE:
        return None
    return CrashSafeLoadGroupScheduling(config, log)


__all__ = ["DIST_MODE", "CrashSafeLoadGroupScheduling", "scheduler_for"]

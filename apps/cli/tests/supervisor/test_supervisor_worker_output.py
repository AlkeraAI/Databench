"""An org worker's output lands in its org's own rotating log, and a worker
that keeps dying is reported on the machine's beat, not only restarted."""

from __future__ import annotations

import os
import socket
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.supervisor.crash_loop import CRASH_LOOP_FAILURES, SETTLED_SECONDS
from alkera_cli.supervisor.launch import WorkerLaunch, plan_launch
from alkera_cli.supervisor.org_worker_log import OrgLog, org_log_path
from alkera_cli.supervisor.service import (
    FileRoutingFeed,
    RouteEntry,
    Supervisor,
    Worker,
)
from alkera_cli.supervisor.slots import SlotTable
from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport

#: A box whose probe proved every mechanism but systemd: namespaced workers,
#: spawned directly.
NAMESPACED = IsolationReport(frozenset(IsolationMechanism) - {IsolationMechanism.SYSTEMD})


ORG_A = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
ORG_B = "be0a3393-082f-4acf-918d-6eac00904c9b"

_CRASHING_WORKER = (
    "import sys\n"
    "print('worker says hello', flush=True)\n"
    "sys.stderr.write('Traceback: the alkera-kernel distribution is not installed\\n')\n"
    "sys.exit(3)\n"
)


def _bare_supervisor(tmp_path: Path) -> Supervisor:
    return Supervisor(
        api=None,  # type: ignore[arg-type]  # the spawn under test never calls it
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        slots=SlotTable(tmp_path / "slots.json"),
        env={},
        isolation=NAMESPACED,
        log_dir=tmp_path / "logs",
    )


async def test_a_worker_that_dies_at_start_leaves_its_output_in_its_orgs_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = _bare_supervisor(tmp_path)
    slot = SlotTable(tmp_path / "slots.json").assign(ORG_A)
    argv = (sys.executable, "-c", _CRASHING_WORKER)
    launch = WorkerLaunch(slot=slot, org_root=tmp_path / "org", argv=argv, env=dict(os.environ))
    # A worker on its own: the launch a box with no namespaces gives it.
    plan = plan_launch(launch, IsolationReport(frozenset()))
    mine, theirs = socket.socketpair()
    try:
        process = await sup._spawn(plan, launch, theirs)
        assert await process.wait() == 3
        await sup._outputs[ORG_A]
    finally:
        mine.close()
        theirs.close()

    path = org_log_path(tmp_path / "logs", ORG_A)
    text = path.read_text()
    assert "worker says hello" in text
    assert "the alkera-kernel distribution is not installed" in text
    assert "exited with status 3" in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # One org's log only: nothing was written under another org's name.
    assert not org_log_path(tmp_path / "logs", ORG_B).exists()


def test_the_org_log_rolls_over_and_keeps_a_bounded_history(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "org-x.log"
    log = OrgLog(path, max_bytes=100, backups=2)
    for index in range(40):
        log.write(f"line {index:02d} {'x' * 20}".encode())
    log.close()
    names = sorted(p.name for p in path.parent.iterdir())
    assert names == ["org-x.log", "org-x.log.1", "org-x.log.2"]
    assert all(p.stat().st_size <= 100 for p in path.parent.iterdir())
    # The newest line is in the live file, an older one in the first backup.
    assert "line 39" in path.read_text()
    assert "line 3" in (path.parent / "org-x.log.1").read_text()


class _Fleet:
    def __init__(self, table: SlotTable) -> None:
        self.table = table
        self.dead: set[str] = set()
        self.refuse: set[str] = set()
        self.now = 0.0
        self.starts = 0

    async def start(self, org_id: str) -> Worker:
        if org_id in self.refuse:
            raise RuntimeError("no namespace")
        self.starts += 1

        async def send(_frame: Any) -> None:
            return None

        return Worker(
            slot=self.table.assign(org_id),
            alive=lambda: org_id not in self.dead,
            send=send,
            started_at=self.now,
        )


def _looping(tmp_path: Path) -> tuple[Supervisor, _Fleet]:
    table = SlotTable(tmp_path / "slots.json")
    fleet = _Fleet(table)
    sup = Supervisor(
        api=None,  # type: ignore[arg-type]
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        slots=table,
        env={"ALKERA_ORG_WORKER_BUDGET": "8"},
        start_worker=fleet.start,
        isolation=NAMESPACED,
        log_dir=tmp_path / "logs",
    )
    sup._clock = lambda: fleet.now
    return sup, fleet


async def _die_and_restart(sup: Supervisor, fleet: _Fleet, *, after: float) -> None:
    """The worker runs ``after`` seconds, dies, and is started again."""
    routes = [RouteEntry("a1", ORG_A)]
    fleet.now += after
    fleet.dead.add(ORG_A)
    await sup.reconcile(routes, now=fleet.now)
    fleet.dead.discard(ORG_A)
    before = fleet.starts
    while fleet.starts == before:
        fleet.now += 1
        await sup.reconcile(routes, now=fleet.now)


async def test_a_worker_that_keeps_dying_soon_after_start_is_reported_failing(
    tmp_path: Path,
) -> None:
    sup, fleet = _looping(tmp_path)
    await sup.reconcile([RouteEntry("a1", ORG_A)], now=0)
    for _ in range(CRASH_LOOP_FAILURES - 1):
        await _die_and_restart(sup, fleet, after=1)
    assert sup.failing == frozenset()
    await _die_and_restart(sup, fleet, after=1)
    assert sup.failing == frozenset({ORG_A})

    from alkera_core.schemas.compute import MachineHeartbeatRequest

    beat = MachineHeartbeatRequest.model_validate(sup.heartbeat_body())
    assert beat.resources is not None and beat.resources.org_workers_failing == 1

    # A worker of the org that then stays up clears it.
    fleet.now += SETTLED_SECONDS
    await sup.reconcile([RouteEntry("a1", ORG_A)], now=fleet.now)
    assert sup.failing == frozenset()
    assert sup.resources()["org_workers_failing"] == 0


async def test_a_worker_that_dies_after_a_long_run_is_not_a_crash_loop(tmp_path: Path) -> None:
    sup, fleet = _looping(tmp_path)
    await sup.reconcile([RouteEntry("a1", ORG_A)], now=0)
    for _ in range(CRASH_LOOP_FAILURES + 1):
        await _die_and_restart(sup, fleet, after=SETTLED_SECONDS + 1)
    assert sup.failing == frozenset()


async def test_an_org_whose_worker_never_starts_is_reported_failing(tmp_path: Path) -> None:
    sup, fleet = _looping(tmp_path)
    fleet.refuse.add(ORG_A)
    routes = [RouteEntry("a1", ORG_A), RouteEntry("b1", ORG_B)]
    for _ in range(200):
        fleet.now += 5
        await sup.reconcile(routes, now=fleet.now)
        if ORG_A in sup.failing:
            break
    assert sup.failing == frozenset({ORG_A})
    assert sup.resources()["org_workers_failing"] == 1


async def test_a_worker_asked_to_stop_is_not_counted_as_failing(tmp_path: Path) -> None:
    sup, fleet = _looping(tmp_path)
    await sup.reconcile([RouteEntry("a1", ORG_A)], now=0)
    for _ in range(CRASH_LOOP_FAILURES):
        await _die_and_restart(sup, fleet, after=1)
    assert ORG_A in sup.failing
    sup.workers[ORG_A].stopping = True
    fleet.dead.add(ORG_A)
    fleet.now += 1
    await sup.reconcile([], now=fleet.now)
    assert sup.failing == frozenset()

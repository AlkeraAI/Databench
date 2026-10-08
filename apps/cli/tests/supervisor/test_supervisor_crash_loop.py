"""What a box says of an org whose worker keeps failing, on both of its
channels at once: the machine's beat counts the org failing after three
failures in a row, and ops get ``supervisor.worker.crash_loop`` from the sixth
failure in fifteen minutes. Driven through the supervisor's reconcile on its
own clock; every time below is worked out from the restart waits (2, 4, 8,
16, 32, 64, then 120 seconds)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.supervisor import org_events
from alkera_cli.supervisor.org_reap import REAP_IDLE_SECONDS, remove_org_data
from alkera_cli.supervisor.service import FileRoutingFeed, RouteEntry, Supervisor, Worker
from alkera_cli.supervisor.slots import SlotTable
from alkera_core.compute import box_logs
from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport
from alkera_core.schemas.compute import MachineHeartbeatRequest
from freezegun import freeze_time

#: A box that keeps orgs apart, spawning workers directly: it may reap one.
NAMESPACED = IsolationReport(frozenset(IsolationMechanism) - {IsolationMechanism.SYSTEMD})

ORG_A = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
ORG_B = "be0a3393-082f-4acf-918d-6eac00904c9b"
ROUTED = [RouteEntry("a1", ORG_A)]


class Box:
    """A supervisor, the workers it starts and the clock they share."""

    def __init__(self, tmp_path: Path) -> None:
        self.now = 0.0
        self.dead: set[str] = set()
        self.refuse: set[str] = set()
        self.started_at: list[float] = []
        self.loops: list[dict[str, Any]] = []
        self.table = SlotTable(tmp_path / "state" / "slots.json")
        self.sup = Supervisor(
            api=None,  # type: ignore[arg-type]  # nothing here claims or beats
            feed=FileRoutingFeed(tmp_path / "routing.json"),
            slots=self.table,
            orgs_root=tmp_path / "orgs",
            env={"ALKERA_ORG_WORKER_BUDGET": "8"},
            start_worker=self._start,
            isolation=NAMESPACED,
            log_dir=tmp_path / "logs",
        )
        self.sup._clock = lambda: self.now
        self.sup._remove_org = lambda orgs, slot: remove_org_data(orgs, slot, mountpoints=list)

    async def _start(self, org_id: str) -> Worker:
        self.started_at.append(self.now)
        if org_id in self.refuse:
            raise RuntimeError("no namespace")

        async def send(_frame: Any) -> None:
            return None

        return Worker(
            slot=self.table.assign(org_id),
            alive=lambda: org_id not in self.dead,
            send=send,
            started_at=self.now,
        )

    async def tick(self, routes: list[RouteEntry] | None = None) -> None:
        await self.sup.reconcile(ROUTED if routes is None else routes, now=self.now)

    async def die_at(self, at: float) -> None:
        """ORG_A's worker is found dead at ``at``."""
        self.now = at
        self.dead.add(ORG_A)
        await self.tick()
        self.dead.discard(ORG_A)

    async def restart(self) -> None:
        """Tick a second at a time until ORG_A's worker is started again."""
        before = len(self.started_at)
        while len(self.started_at) == before:
            self.now += 1.0
            await self.tick()

    async def die_soon_and_restart(self) -> None:
        await self.die_at(self.now + 1.0)
        await self.restart()

    def failing_on_the_beat(self) -> int:
        beat = MachineHeartbeatRequest.model_validate(self.sup.heartbeat_body())
        assert beat.resources is not None
        return beat.resources.org_workers_failing


@pytest.fixture
def box(tmp_path: Path) -> Iterator[Box]:
    box = Box(tmp_path)

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.msg == box_logs.WORKER_CRASH_LOOP:
                fields = getattr(record, "event_fields", {})
                box.loops.append({"log_level": record.levelname.lower(), **fields})

    log = logging.getLogger(org_events.LOGGER)
    keep, level = Keep(), log.level
    log.addHandler(keep)
    log.setLevel(logging.DEBUG)
    try:
        yield box
    finally:
        log.removeHandler(keep)
        log.setLevel(level)


async def test_a_worker_dying_a_second_after_each_start_is_failing_at_three_and_a_loop_at_six(
    box: Box,
) -> None:
    await box.tick()
    failing: list[int] = []
    alarms: list[int] = []
    for _ in range(7):
        await box.die_soon_and_restart()
        failing.append(box.failing_on_the_beat())
        alarms.append(len(box.loops))
    # It dies at 1, 4, 9, 18, 35, 68 and 133 seconds: all inside one window.
    assert box.started_at == [0.0, 3.0, 8.0, 17.0, 34.0, 67.0, 132.0, 253.0]
    assert failing == [0, 0, 1, 1, 1, 1, 1]
    assert alarms == [0, 0, 0, 0, 0, 1, 2]
    assert box.sup.failing == frozenset({ORG_A})
    assert box.loops == [
        {
            "log_level": "error",
            "slot": 0,
            "org_id": ORG_A,
            "restarts": 6,
            "window": 900.0,
            "backoff": 64.0,
            # Each exit is the worker dying soon after its start.
            "reason": "exited",
        },
        {
            "log_level": "error",
            "slot": 0,
            "org_id": ORG_A,
            "restarts": 7,
            "window": 900.0,
            "backoff": 120.0,
            # Each exit is the worker dying soon after its start.
            "reason": "exited",
        },
    ]


async def test_a_worker_dying_two_minutes_after_each_start_is_a_loop_but_never_failing(
    box: Box,
) -> None:
    """Each run settles, so the streak never passes one and the wait stays at
    its floor; the sixth death in fifteen minutes still alarms."""
    await box.tick()
    failing: list[int] = []
    for _ in range(6):
        await box.die_at(box.now + 120.0)
        failing.append(box.failing_on_the_beat())
        await box.restart()
    # Dead at 120, 242, 364, 486, 608 and 730 seconds.
    assert box.started_at == [0.0, 122.0, 244.0, 366.0, 488.0, 610.0, 732.0]
    assert failing == [0] * 6
    assert [(e["restarts"], e["backoff"]) for e in box.loops] == [(6, 2.0)]


@pytest.mark.parametrize(
    ("dies_at", "restarts"),
    [
        pytest.param(900.0, [6, 7], id="every-failure-still-in-the-window"),
        pytest.param(903.0, [6, 6], id="the-first-has-aged-out"),
        pytest.param(904.0, [6], id="the-second-aged-out-too-so-no-loop"),
        pytest.param(5000.0, [6], id="long-after"),
    ],
)
async def test_failures_age_out_of_the_window_and_a_settled_worker_stops_the_beat_count(
    box: Box, dies_at: float, restarts: list[int]
) -> None:
    await box.tick()
    for _ in range(6):
        await box.die_soon_and_restart()
    # Dead at 1, 4, 9, 18, 35 and 68 seconds; up again at 132.
    assert box.now == 132.0 and box.failing_on_the_beat() == 1
    box.now = 191.0
    await box.tick()
    assert box.failing_on_the_beat() == 1, "59 seconds up is not settled"
    box.now = 192.0
    await box.tick()
    assert box.failing_on_the_beat() == 0
    await box.die_at(dies_at)
    assert [e["restarts"] for e in box.loops] == restarts
    assert box.sup.failing == frozenset(), "one death after a settled run"


async def test_an_org_whose_worker_never_starts_is_failing_at_three_tries_and_a_loop_at_six(
    box: Box,
) -> None:
    box.refuse.add(ORG_A)
    first_failing: float | None = None
    for second in range(70):
        box.now = float(second)
        await box.tick()
        if first_failing is None and box.failing_on_the_beat() == 1:
            first_failing = box.now
    assert box.started_at == [0.0, 2.0, 6.0, 14.0, 30.0, 62.0]
    assert first_failing == 6.0
    # No slot was ever held, and the event says so.
    assert [(e["slot"], e["org_id"], e["restarts"]) for e in box.loops] == [(None, ORG_A, 6)]


async def test_a_worker_asked_to_stop_is_no_failure_but_what_failed_before_still_counts(
    box: Box,
) -> None:
    await box.tick()
    for _ in range(5):
        await box.die_soon_and_restart()
    assert box.failing_on_the_beat() == 1 and box.loops == []
    box.sup.workers[ORG_A].stopping = True
    await box.die_at(box.now + 1.0)
    assert box.failing_on_the_beat() == 0 and box.loops == []
    # A stop owes no wait: the same pass started the org's next worker.
    assert box.started_at[-1] == box.now
    await box.die_at(box.now + 1.0)
    # The sixth unasked exit in the window; the first of a new streak.
    assert [e["restarts"] for e in box.loops] == [6]
    assert box.failing_on_the_beat() == 0


async def test_an_org_reaped_from_the_box_is_no_longer_counted_failing(box: Box) -> None:
    start = datetime(2026, 9, 1, 12, tzinfo=UTC)
    elsewhere = [RouteEntry("b1", ORG_B)]
    with freeze_time(start, real_asyncio=True) as frozen:
        await box.tick()
        for _ in range(2):
            await box.die_soon_and_restart()
        await box.die_at(box.now + 1.0)
        assert box.sup.failing == frozenset({ORG_A})
        # ORG_A's chats move to another box; much later its data is removed.
        frozen.move_to(start + timedelta(seconds=REAP_IDLE_SECONDS) + timedelta(days=1))
        box.now = 1_000_000.0
        await box.tick(elsewhere)
    assert box.table.find(ORG_A) is None, "reaped"
    assert box.sup.failing == frozenset()
    assert box.failing_on_the_beat() == 0


@contextlib.contextmanager
def _error_lines(name: str) -> Iterator[list[str]]:
    seen: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.ERROR:
                seen.append(record.getMessage())

    log = logging.getLogger(name)
    keep = Keep()
    log.addHandler(keep)
    try:
        yield seen
    finally:
        log.removeHandler(keep)


async def test_the_supervisor_names_the_orgs_log_once_when_its_worker_begins_failing(
    box: Box, tmp_path: Path
) -> None:
    with _error_lines("alkera_cli.supervisor.service") as lines:
        await box.tick()
        for _ in range(5):
            await box.die_soon_and_restart()
    assert len(lines) == 1
    assert "3 failures in a row" in lines[0]
    assert str(tmp_path / "logs") in lines[0]

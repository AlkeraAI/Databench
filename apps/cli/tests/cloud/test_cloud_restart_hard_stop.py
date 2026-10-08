"""A supervised restart ends the process within a bound.

On the demo box the daemon announced its restart, waited its drain ceiling,
and then sat for nine minutes: the interpreter waits at exit for worker
threads, and some never returned. The machine was out of placement the whole
time and every new chat waited with it. A restart now arms a hard stop past
the ceiling that dumps every thread's stack to the log and ends the process,
so the supervisor starts the next one at once.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _mirror_service import Clock, build_service
from alkera_cli.cloud import service as service_module
from alkera_cli.cloud.service import (
    DEFAULT_DRAIN_CEILING_SECONDS,
    RESTART_HARD_STOP_SECONDS,
    arm_hard_stop,
    stop_hard,
)
from alkera_core.compute.box_contract import BootstrapPlan, StartMode
from alkera_core.compute.liveness import ENV_DRAIN_CEILING_SECONDS, STOP_EXIT_SECONDS

#: What every render here installs and starts, unless a test says otherwise.
PLAN = BootstrapPlan(version="1.4.2", start_mode=StartMode.SUPERVISE)


class Armed:
    """Records what the service asked to happen, and when."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, Callable[[], None]]] = []

    def __call__(self, seconds: float, action: Callable[[], None]) -> None:
        self.calls.append((seconds, action))


def _service(tmp_path: Path, *, supervised: bool) -> tuple[Any, Armed]:
    armed = Armed()
    service, _ = build_service(tmp_path, clock=Clock(), hard_stop=armed, supervised=supervised)
    return service, armed


def test_a_supervised_restart_arms_a_hard_stop_past_the_drain_ceiling(tmp_path: Path) -> None:
    """A restart may wait for a background job up to the drain ceiling, so its
    backstop lies past that; once the wait is over, a second, short one bounds
    the exit, so a restart that hangs after waiting for nothing is not left for
    the ceiling."""
    service, armed = _service(tmp_path, supervised=True)
    service.begin_drain()
    assert [seconds for seconds, _ in armed.calls] == [
        service._settings.drain_ceiling_seconds + RESTART_HARD_STOP_SECONDS
    ]
    # Said once: a second drain request does not arm a second stop.
    service.begin_drain()
    assert len(armed.calls) == 1
    asyncio.run(service._wait_for_in_flight())
    assert [seconds for seconds, _ in armed.calls][1:] == [RESTART_HARD_STOP_SECONDS]


@pytest.mark.parametrize(
    "ceiling",
    [
        pytest.param(0.0, id="a-drain-that-hands-back-at-once"),
        pytest.param(30.0, id="a-short-configured-ceiling"),
        pytest.param(DEFAULT_DRAIN_CEILING_SECONDS, id="the-platform-default"),
    ],
)
def test_an_ordinary_stop_arms_a_hard_stop_past_its_own_drain_ceiling(
    tmp_path: Path,
    ceiling: float,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unsupervised stop (``systemctl restart`` on an EC2 node) hands every
    chat back and may take as long as its drain ceiling allows — and no longer.
    With no timer, a process whose exit waited on a worker thread parked on the
    drive sat in its stop for as long as anybody watched, its chats served by
    nobody and handed to no other box. Nothing ends the drain early: the timer
    is past the ceiling by the stop's own margin."""
    armed = Armed()
    service, _ = build_service(
        tmp_path,
        clock=Clock(),
        hard_stop=armed,
        supervised=False,
        drain_ceiling_seconds=ceiling,
    )
    service.begin_drain()
    assert not service.restarting
    assert [seconds for seconds, _ in armed.calls] == [ceiling + STOP_EXIT_SECONDS]
    service.begin_drain()
    assert len(armed.calls) == 1, "a second stop signal arms no second timer"

    ((_, action),) = armed.calls
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(
        service_module.faulthandler, "dump_traceback", lambda all_threads=False: None
    )
    with caplog.at_level(logging.WARNING, logger=service_module.logger.name):
        action()
    assert exits == [0], "the timer ends the process so its supervisor starts the next"
    assert any("the stop did not end the process within" in r.getMessage() for r in caplog.records)


def test_the_hard_stop_dumps_every_thread_and_ends_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service, armed = _service(tmp_path, supervised=True)
    service.begin_drain()
    ((_, action),) = armed.calls

    exits: list[int] = []
    dumps: list[bool] = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(
        service_module.faulthandler,
        "dump_traceback",
        lambda all_threads=False: dumps.append(all_threads),
    )
    with caplog.at_level(logging.WARNING, logger=service_module.logger.name):
        action()
    assert exits == [0]
    assert dumps == [True], "the stacks of every thread are the one clue a hang leaves"
    assert any("did not end the process within" in rec.getMessage() for rec in caplog.records)


def test_the_default_seam_fires_on_its_own_thread() -> None:
    fired = threading.Event()
    timer = arm_hard_stop(0.05, fired.set)
    assert timer.daemon, "a stop timer must never itself keep the process alive"
    assert fired.wait(2.0), "the timer never fired"


def test_stop_hard_says_why_before_it_ends(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(
        service_module.faulthandler, "dump_traceback", lambda all_threads=False: None
    )
    with caplog.at_level(logging.WARNING, logger=service_module.logger.name):
        stop_hard("the test asked")
    assert exits == [0]
    assert any(rec.getMessage().startswith("the test asked;") for rec in caplog.records)


def test_stop_hard_ends_the_process_even_when_a_log_handler_has_lost_its_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler over a closed file raises on flush — the state a handler is
    left in once whoever captured stderr has closed its stream. The stop must
    still dump the stacks and end the process: staying alive because the log
    could not be flushed is the failure this function exists to prevent."""
    exits: list[int] = []
    dumps: list[bool] = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(
        service_module.faulthandler,
        "dump_traceback",
        lambda all_threads=False: dumps.append(all_threads),
    )
    closed = (tmp_path / "stderr.log").open("w")
    closed.close()
    handler = logging.StreamHandler(closed)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        stop_hard("the test asked")
    finally:
        root.removeHandler(handler)
    assert dumps == [True]
    assert exits == [0]


def test_a_lifecycle_test_service_arms_no_real_timer_on_a_supervised_restart(
    tmp_path: Path,
) -> None:
    """``build_service`` without a hard-stop seam of its own must not arm the
    production timer: that timer ends the process — here, the pytest worker —
    ninety seconds later, inside whatever test is running by then."""
    timers_before = {t for t in threading.enumerate() if isinstance(t, threading.Timer)}
    service, _built = build_service(tmp_path, clock=Clock(), supervised=True)
    try:
        service.begin_drain()
        armed = [
            t
            for t in threading.enumerate()
            if isinstance(t, threading.Timer) and t not in timers_before
        ]
    finally:
        for timer in [t for t in threading.enumerate() if isinstance(t, threading.Timer)]:
            if timer not in timers_before:
                timer.cancel()
    assert service.restarting, "the drain was a supervised restart"
    assert armed == [], "a lifecycle test armed the timer that ends the process"


@pytest.mark.parametrize("ceiling", [0, 900, 6 * 60 * 60])
def test_the_node_unit_outwaits_the_daemons_own_hard_stop_on_the_ceiling_it_is_given(
    ceiling: int,
) -> None:
    """The node's service unit and the daemon read one ceiling: the value the
    bootstrap writes into the node's environment is the one the daemon drains
    by, and the unit's stop timeout is past the daemon's own hard stop on it —
    so the daemon always ends itself first, and the kill is only the backstop."""
    from alkera_core.compute.bootstrap import BootstrapSpec, render_bootstrap

    script = render_bootstrap(
        BootstrapSpec(
            provider="ec2",
            allocation_id="00000000-0000-0000-0000-000000000001",
            machine_name="pool-1",
            type_code="m6i.large",
            tenancy="pool",
            api_url="https://api.example.test",
            release_base_url="https://releases.example.test/",
            credential_secret="alkera/test/node/1",
            region="us-east-1",
            drain_ceiling_seconds=ceiling,
        ),
        PLAN,
    )
    env = dict(
        line.split("=", 1)
        for line in script.splitlines()
        if line.startswith(ENV_DRAIN_CEILING_SECONDS + "=")
    )
    daemon_ceiling = service_module.drain_ceiling_from_env(env)
    assert daemon_ceiling == ceiling
    (timeout,) = [
        int(line.split("=", 1)[1])
        for line in script.splitlines()
        if line.startswith("TimeoutStopSec=")
    ]
    assert timeout > daemon_ceiling + RESTART_HARD_STOP_SECONDS
